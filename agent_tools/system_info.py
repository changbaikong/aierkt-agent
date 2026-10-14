"""Safe, read-only local machine information."""

from __future__ import annotations

import os
import platform
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.tools import classify_tool_error


def _local_timezone() -> str:
    value = datetime.now().astimezone().tzinfo
    return getattr(value, "key", None) or str(value) or "UTC"


def _read_local_time() -> dict:
    now = datetime.now().astimezone()
    return {
        "local_time": now.isoformat(timespec="seconds"),
        "timezone": _local_timezone(),
        "source": "local",
    }


def local_time_info() -> dict:
    """Read the local clock without falling back to any network source."""
    try:
        return {"status": "ok", "output": _read_local_time()}
    except Exception as exc:
        return {"status": "failed", "error": f"local time unavailable: {type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}


def make_system_info_tool() -> ToolSpec:
    def handler(arguments: dict, _scope: Scope) -> dict:
        kind = str(arguments.get("kind") or "all")
        if kind not in {"all", "time", "platform", "host", "cwd"}:
            return {"status": "failed", "error": "kind must be one of all, time, platform, host, cwd", "error_kind": "permanent"}
        time_result = local_time_info()
        if time_result["status"] != "ok":
            return time_result
        time_output = dict(time_result["output"])
        values = {
            "time": time_output,
            "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "source": "local"},
            # host 是"Agent 自己住在什么环境里"，回答者需要系统名 + 主机名。
            # 只给 hostname 的话，上层没法说清"我住在你这台 Windows 电脑里"。
            "host": {"system": platform.system(), "hostname": platform.node(), "source": "local"},
            "cwd": {"path": str(Path.cwd()), "source": "local"},
        }
        if kind == "all":
            output = {key: value for key, item in values.items() for key, value in item.items()}
            output["source"] = "local"
            return {"status": "ok", "output": output}
        return {"status": "ok", "output": values[kind]}

    return ToolSpec(
        name="local_info",
        schema={"type": "function", "function": {"name": "local_info", "description": "Read safe local machine information", "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["all", "time", "platform", "host", "cwd"]}}}}},
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    )
