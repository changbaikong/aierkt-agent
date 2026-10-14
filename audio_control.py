"""Optional Windows master-volume control with an explicit unavailable state."""
from __future__ import annotations

from typing import Any


def _backend() -> tuple[Any, str]:
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
        from comtypes import CLSCTX_ALL  # type: ignore
        device = None
        for candidate in getattr(AudioUtilities, "GetAllDevices", lambda: [])():
            candidate_name = str(getattr(candidate, "FriendlyName", None) or getattr(candidate, "friendly_name", None) or "")
            if "realtek" in candidate_name.lower():
                device = candidate
                break
        device = device or AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = interface.QueryInterface(IAudioEndpointVolume)
        name = getattr(device, "FriendlyName", None) or getattr(device, "friendly_name", None) or "系统默认音频设备"
        return endpoint, str(name)
    except Exception:
        return None, ""


def available() -> dict[str, Any]:
    endpoint, device_name = _backend()
    return {"available": endpoint is not None, "backend": "pycaw" if endpoint is not None else "unavailable",
            "device": device_name, "is_realtek": "realtek" in device_name.lower(),
            "reason": "no active Windows audio endpoint" if endpoint is None else ""}


def adjust_volume(percent: float) -> dict[str, Any]:
    endpoint, device_name = _backend()
    if endpoint is None:
        return {"status": "unavailable", "message": "当前环境未安装可用的 Windows 音量控制后端（需要 pycaw/comtypes）"}
    current = float(endpoint.GetMasterVolumeLevelScalar())
    target = max(0.0, min(1.0, current + float(percent) / 100.0))
    endpoint.SetMasterVolumeLevelScalar(target, None)
    return {"status": "ok", "device": device_name, "is_realtek": "realtek" in device_name.lower(), "before_percent": round(current * 100, 1), "after_percent": round(target * 100, 1)}
