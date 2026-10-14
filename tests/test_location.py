import pytest
from datetime import datetime, timedelta, timezone

from agent_core.contracts import Scope
from agent_tools.location import LocationStore, make_location_tool


def test_location_store_shares_across_conversations_within_one_account() -> None:
    """位置按**账号 + 渠道**保存：换个会话不该让用户重新授权一次。

    这是 2026-09-20 的行为变更。旧实现按会话隔离，测试断言的是
    "另一个会话读不到"；那个前提与"刷新页面、切换会话不该丢位置"直接冲突，
    所以隔离维度改为 (owner_id, channel, module_id)。
    """
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = LocationStore(ttl_seconds=300, now=lambda: now)
    first = Scope(owner_id="a", channel="web", conversation_id="one")
    second = Scope(owner_id="a", channel="web", conversation_id="two")

    snapshot = store.update(first, 31.2304, 121.4737, 18.5, now)

    assert snapshot["latitude"] == 31.2304
    assert store.get(first)["longitude"] == 121.4737
    # 同一账号同一渠道、不同会话 —— 共享同一条位置
    assert store.get(second)["longitude"] == 121.4737


def test_location_store_isolates_other_accounts_and_channels() -> None:
    """账号级不等于"谁都能读"：换账号、换渠道仍然互相看不见。"""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = LocationStore(ttl_seconds=300, now=lambda: now)
    store.update(Scope(owner_id="a", channel="web"), 31.2304, 121.4737, 18.5, now)

    assert store.get(Scope(owner_id="b", channel="web")) is None
    assert store.get(Scope(owner_id="a", channel="cli")) is None


def test_location_store_marks_expired_instead_of_dropping() -> None:
    """过期不删：留着才能区分"没开共享"和"开过但过期了"。"""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    current = [now]
    store = LocationStore(ttl_seconds=300, now=lambda: current[0])
    scope = Scope()
    store.update(scope, 0, 0, None, now)

    current[0] = now + timedelta(seconds=301)
    expired = store.get(scope)
    assert expired is not None, "过期快照必须仍然可见，否则 stale 分支不可达"
    assert expired["expired"] is True
    assert expired["age_seconds"] == 301.0

    # 重新上报后恢复为有效
    store.update(scope, 1.0, 2.0, None, current[0])
    fresh = store.get(scope)
    assert fresh["expired"] is False

    # 显式清理仍能删除
    assert store.clear(scope) is True
    assert store.get(scope) is None


@pytest.mark.asyncio
async def test_location_tool_reports_stale_when_snapshot_expired() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    current = [now]
    store = LocationStore(ttl_seconds=300, now=lambda: current[0])
    tool = make_location_tool(store)
    scope = Scope()
    store.update(scope, 31.2304, 121.4737, 18.5, now)

    current[0] = now + timedelta(seconds=900)
    result = await tool.handler({}, scope)

    assert result["status"] == "stale"
    assert result["output"]["reason"] == "location_stale"
    # 过期位置不具备"你现在在哪"的可信度，坐标不能交出去
    assert "latitude" not in result["output"]
    assert "longitude" not in result["output"]
    assert "31.2304" not in str(result)


def test_location_store_rejects_invalid_coordinates() -> None:
    store = LocationStore()
    with pytest.raises(ValueError, match="latitude"):
        store.update(Scope(), 91, 0, None, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_location_tool_reports_unavailable_without_position() -> None:
    tool = make_location_tool(LocationStore())

    result = await tool.handler({}, Scope())

    assert result["status"] == "unavailable"
    assert result["output"]["reason"] == "location_unavailable"


@pytest.mark.asyncio
async def test_location_tool_refuses_bare_coordinates_without_resolver() -> None:
    """没接解析能力时**不能**把裸坐标交出去。

    裸经纬度对上游没有可用价值（observation 层本来就会剥掉），
    而它一旦留在工具输出里，就随时可能被模型复述进气泡。
    """
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = LocationStore(ttl_seconds=300, now=lambda: now)
    tool = make_location_tool(store)
    scope = Scope()
    store.update(scope, 31.2304, 121.4737, 18.5, now)

    result = await tool.handler({}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "location_resolution_unavailable"
    assert "latitude" not in result["output"]
    assert "31.2304" not in str(result)


@pytest.mark.asyncio
async def test_location_tool_accepts_async_resolver_and_reports_city() -> None:
    """生产走的是异步解析（高德逆地理），这里用协程解析器验证这条真链路。"""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = LocationStore(ttl_seconds=300, now=lambda: now)
    scope = Scope()

    async def resolver(snapshot: dict) -> dict:
        assert snapshot["latitude"] == 31.2304
        return {
            "province": "上海市", "city": "上海市", "district": "浦东新区",
            "township": "陆家嘴街道", "area_code": "310115",
        }

    tool = make_location_tool(store, resolver)
    store.update(scope, 31.2304, 121.4737, 80, now)
    result = await tool.handler({}, scope)

    assert result["status"] == "ok"
    assert result["output"]["city"] == "上海市"
    assert result["output"]["area_code"] == "310115"
    assert result["output"]["precision"] == "street"
    assert "latitude" not in result["output"]
    assert "longitude" not in result["output"]


@pytest.mark.asyncio
async def test_location_tool_hides_resolver_failure_details() -> None:
    """解析挂了也要给稳定码，外部服务的原文（HTTP 码之类）不能漏出去。"""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = LocationStore(ttl_seconds=300, now=lambda: now)
    scope = Scope()

    def boom(_snapshot: dict) -> dict:
        raise RuntimeError("Amap request failed with HTTP 500")

    tool = make_location_tool(store, boom)
    store.update(scope, 31.2304, 121.4737, 18.5, now)
    result = await tool.handler({}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "location_resolution_unavailable"
    assert "HTTP 500" not in str(result)
    assert "Amap" not in str(result)
