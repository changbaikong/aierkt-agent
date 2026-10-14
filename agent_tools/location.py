"""Ephemeral, scope-isolated browser location state and Agent tool."""

from __future__ import annotations

import asyncio
import inspect
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from threading import RLock
from typing import Any, Callable

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec

logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("captured_at must include timezone")
    return value.astimezone(timezone.utc)


class LocationStore:
    """浏览器上报的位置快照，按**账号 + 渠道**保存。

    为什么键里不再有 ``conversation_id``：位置回答的是"这台设备现在在哪"，
    跟用户在哪个会话里说话无关。按会话存会造出一个对用户毫无道理的规则 ——
    在 A 会话开过共享，新开一个标签页（= 新会话）还得再开一次。
    刷新页面、切换会话都不该让服务端位置消失（用户主动关闭除外）。

    ``module_id`` 留在键里，只是不与别的能力抢同一个槽位。

    过期**不删**只标 ``expired=True``，这一条没变，理由见 ``get()``。
    """

    def __init__(self, *, ttl_seconds: int = 300, now: Callable[[], datetime] | None = None) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._values: dict[tuple[str, str, str], dict] = {}
        self._lock = RLock()

    @staticmethod
    def _key(scope: Scope) -> tuple[str, str, str]:
        """账号 + 渠道 + 模块 —— 刻意不含 ``conversation_id``。"""
        return (scope.owner_id, scope.channel, scope.module_id)

    def update(
        self,
        scope: Scope,
        latitude: float,
        longitude: float,
        accuracy_m: float | None,
        captured_at: datetime,
    ) -> dict:
        latitude = float(latitude)
        longitude = float(longitude)
        if not math.isfinite(latitude) or not -90 <= latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")
        if accuracy_m is not None:
            accuracy_m = float(accuracy_m)
            if not math.isfinite(accuracy_m) or accuracy_m < 0:
                raise ValueError("accuracy_m must be a non-negative number")
        captured = _utc(captured_at)
        current = _utc(self.now())
        if captured > current + timedelta(seconds=60):
            raise ValueError("captured_at cannot be more than 60 seconds in the future")
        snapshot = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy_m,
            "captured_at": captured.isoformat(timespec="seconds"),
            "source": "browser",
        }
        with self._lock:
            self._values[self._key(scope)] = snapshot
        return deepcopy(snapshot)

    def get(self, scope: Scope) -> dict | None:
        """读当前位置快照。

        过期**不删**，而是照常返回并标记 ``expired=True``。原因：
        "从没开过共享"和"开过但太久没更新"对用户是两件事 —— 前者要他去开，
        后者只要重新共享一次。删掉记录就把两者合并成了一句"没开共享"，
        用户会以为自己没授权过。过期记录留到下一次成功 update 或显式 clear。

        返回 ``None`` 只表示"这个 scope 从没有过位置"。
        """
        with self._lock:
            value = self._values.get(self._key(scope))
            if value is None:
                return None
            captured = datetime.fromisoformat(value["captured_at"])
            age = (_utc(self.now()) - captured).total_seconds()
            result = deepcopy(value)
            result["age_seconds"] = round(age, 3)
            result["expired"] = age < 0 or age > self.ttl_seconds
            return result

    def clear(self, scope: Scope) -> bool:
        with self._lock:
            return self._values.pop(self._key(scope), None) is not None


def make_location_tool(
    store: LocationStore,
    resolver: Callable[[dict], Any] | None = None,
) -> ToolSpec:
    """位置工具。

    ``resolver`` 负责把坐标快照解析成行政区
    （``province`` / ``city`` / ``district`` / ``township`` / ``area_code``）。
    生产用的是高德逆地理，是**协程**；测试里传的是同步 lambda。两种都接 ——
    用 ``inspect.isawaitable`` 判断，不逼调用方把同步实现也包成协程。

    **没有 resolver 时不把坐标交出去。** 裸经纬度对上游没有可用价值
    （loop 的 observation 层本来就会剥掉它），而它一旦出现在工具输出里，
    就随时可能被模型复述进气泡。所以这里返回明确的失败码，
    让措辞层去说"位置能力没配好"，而不是退化成一条坐标。

    这个函数是 ``async def``：解析要发网络请求，同步签名会把事件循环占住。
    """

    async def handler(_arguments: dict, scope: Scope) -> dict:
        snapshot = store.get(scope)
        if snapshot is None:
            # 这个 scope 从没上报过位置 —— 用户需要去开共享。
            return {
                "status": "unavailable",
                "output": {"reason": "location_unavailable"},
                "error": "No recent browser location is available.",
            }
        if snapshot.get("expired"):
            # 上报过但已超出 TTL —— 用户只要重新共享一次。
            # 坐标**有意不返回**：过期位置不具备"你现在在哪"的可信度，
            # 交出去只会诱使上游把它当当前位置用。
            return {
                "status": "stale",
                "output": {
                    "reason": "location_stale",
                    "age_seconds": snapshot.get("age_seconds"),
                },
                "error": "The shared browser location is stale and needs to be refreshed.",
            }
        if resolver is None:
            # 能力没接上（比如没配高德 key）。说清楚是"解析不了"，
            # 而不是悄悄把裸坐标交出去。
            return {
                "status": "failed",
                "output": {"reason": "location_resolution_unavailable"},
                "error": "location_resolution_unavailable",
            }
        try:
            place = resolver(snapshot)
            if inspect.isawaitable(place):
                place = await place
            place = dict(place or {})
        except asyncio.TimeoutError:
            # 逆地理有自己的超时（在 resolver 内部）。超时是"服务没应"，
            # 不是"用户没开共享"，两者对用户是不同的一句话。
            logger.warning("location resolution timed out")
            return {
                "status": "failed",
                "output": {"reason": "location_resolution_unavailable"},
                "error": "location_resolution_unavailable",
                "error_kind": "transient",
            }
        except Exception as exc:
            # 原文只进日志：这是外部服务内部细节（HTTP 码、API 片段）露头的唯一地方。
            logger.warning("location resolution failed: %s: %s", type(exc).__name__, exc)
            return {
                "status": "failed",
                "output": {"reason": "location_resolution_unavailable"},
                "error": "location_resolution_unavailable",
                "error_kind": "transient",
            }
        accuracy = snapshot.get("accuracy_m")
        try:
            accuracy_value = float(accuracy)
        except (TypeError, ValueError):
            accuracy_value = float("inf")
        precision = "street" if accuracy_value <= 100 and place.get("township") else "district" if accuracy_value <= 1000 and place.get("district") else "city"
        province = str(place.get("province") or "")
        city = str(place.get("city") or "")
        parts = [province] if city and city == province else [province, city]
        if precision in {"district", "street"}:
            parts.append(str(place.get("district") or ""))
        if precision == "street":
            parts.append(str(place.get("township") or ""))
        output = {
            "place_label": "".join(item for item in parts if item),
            "province": province,
            "city": city,
            "area_code": str(place.get("area_code") or ""),
            "precision": precision,
            # 精度与新鲜度给的是**原始数字**，不是分好档的中文 ——
            # `fastpath.location_summary` 才是这类措辞的唯一来源。
            # 工具层再自己写一套"大约百米内"，两套话术迟早会对不上。
            "accuracy_m": accuracy_value if math.isfinite(accuracy_value) else None,
            "age_seconds": float(snapshot.get("age_seconds") or 0),
        }
        if precision in {"district", "street"} and place.get("district"):
            output["district"] = str(place["district"])
        if precision == "street" and place.get("township"):
            output["township"] = str(place["township"])
        return {"status": "ok", "output": output}

    return ToolSpec(
        name="current",
        schema={
            "type": "function",
            "function": {
                "name": "current",
                "description": "Read the user's current place (city / district / area code). The user must have explicitly enabled location sharing.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
        # 方案第一部分要求的"坐标转城市有单独超时"：解析器内部先超（约 3.5 秒），
        # 这里再兜一层，保证"首次解析正常情况下 4 秒内返回"。
        timeout_seconds=6.0,
    )
