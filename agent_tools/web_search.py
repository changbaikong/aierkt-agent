"""即插即用的联网搜索工具（一个文件 = 一个完整工具）。

自包含四件事，全部放在本模块，不往 agent_core 里塞任何东西：
1. 数据 + 接口契约   ``SearchResult`` / ``WebSearchProvider``（可替换供应商）
2. BoCha 适配器       ``BoChaWebSearchProvider``（仿 bigmodel_embeddings 惯例）
3. schema + 校验     ``WEB_SEARCH_SCHEMA`` / ``_validated_args``
4. handler + 工厂     ``make_web_search_tool`` / ``build_web_search_tool``

只依赖 agent_core 的稳定公开契约（``ToolSpec``、``Scope``），
以及第三方 httpx/dotenv（项目已有依赖）。拔掉本文件 = 整个工具消失，主体零残留。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx
from dotenv import load_dotenv

from agent_core.contracts import EFFECT_READ, IntentPolicy, Scope, ToolSpec
from agent_core.project_env import PROJECT_ENV_PATH
from agent_core.tools import classify_tool_error
from .research_scope import ResearchScope
from .agent_reach_router import AgentReachRouter

DEFAULT_BASE_URL = "https://api.bochaai.com/v1"
DEFAULT_TIMEOUT = 20.0
BING_RSS_BASE_URL = "https://www.bing.com/search"
BING_RSS_TIMEOUT = 8.0

MAX_QUERY_LENGTH = 500
SCHEMA_QUERY_LENGTH = 2000
SUBAGENT_QUERY_LENGTH = 200
MAX_RESULTS_MIN = 1
MAX_RESULTS_MAX = 10
MAX_RESULTS_DEFAULT = 5
FRESHNESS_OPTIONS = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")


def _compact_subagent_query(query: str, scope: Scope) -> str:
    """Keep delegated research searches focused without changing normal searches."""
    if (
        "/sub" not in str(scope.module_id)
        or (len(query) <= SUBAGENT_QUERY_LENGTH and "\n" not in query)
    ):
        return query
    quoted = re.findall(r"[`\"'“”「」]([^`\"'“”「」]{2,80})[`\"'“”「」]", query)
    if quoted:
        return " ".join(quoted[:3])[:SUBAGENT_QUERY_LENGTH].rstrip()
    latin_terms = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[ /-]+[A-Za-z][A-Za-z0-9]*)*", query)
    latin_terms = [
        term.strip(" /-") for term in latin_terms
        if term.lower() not in {"task", "search", "web", "output", "format"}
    ]
    if latin_terms:
        return " ".join(latin_terms)[:SUBAGENT_QUERY_LENGTH].rstrip()
    lines = [line.strip() for line in query.splitlines() if line.strip()]
    candidates: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^(?:任务|要求|目标|步骤|输出格式)\s*[:：]\s*", "", line)
        if not cleaned or re.match(r"^\d+[.、)]", cleaned):
            continue
        if cleaned.startswith(("只读研究", "只做只读", "只读，不", "不修改任何")):
            continue
        candidates.append(cleaned)
    compacted = (candidates or lines or [query])[0]
    return compacted[:SUBAGENT_QUERY_LENGTH].rstrip()


# ---------------------------------------------------------------------------
# 1. 数据 + 接口契约（工具内部自持，供应商可替换）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    summary: str = ""
    platform: str = "web"
    published_at: str = ""


class WebSearchProvider(Protocol):
    """任何实现 search() 的对象都可作为工具后端（BoCha / 测试 fake / 未来其他供应商）。"""

    def search(
        self,
        query: str,
        *,
        max_results: int = MAX_RESULTS_DEFAULT,
        freshness: str = "noLimit",
    ) -> list[SearchResult]: ...


# ---------------------------------------------------------------------------
# 2. BoCha 适配器（可选依赖：未配置 key 时优雅降级，不抛异常）
# ---------------------------------------------------------------------------
class BoChaWebSearchProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        summary: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.summary = summary
        self.transport = transport  # 测试注入 httpx.MockTransport 用

    @classmethod
    def from_env(cls, env_path: str | Path = PROJECT_ENV_PATH) -> "BoChaWebSearchProvider | None":
        """从 .env 读取配置。无 BOCHA_API_KEY 时返回 None（可选依赖降级，不抛）。

        与 embedding 的 from_env 区别：embedding 是必配依赖（缺 key 抛 RuntimeError），
        工具是可插拔可选依赖（缺 key 返回 None，主体跳过注册）。
        """
        load_dotenv(dotenv_path=Path(env_path), override=False)
        api_key = os.environ.get("BOCHA_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            base_url=os.environ.get("BOCHA_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            timeout=float(os.environ.get("BOCHA_TIMEOUT", str(DEFAULT_TIMEOUT)) or DEFAULT_TIMEOUT),
            summary=os.environ.get("BOCHA_SUMMARY", "true").strip().lower() != "false",
        )

    def search(
        self,
        query: str,
        *,
        max_results: int = MAX_RESULTS_DEFAULT,
        freshness: str = "noLimit",
    ) -> list[SearchResult]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "query": query,
            "count": max_results,
            "freshness": freshness,
            "summary": self.summary,
        }
        with httpx.Client(timeout=self.timeout, trust_env=False, transport=self.transport) as client:
            response = client.post(f"{self.base_url}/web-search", headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"BoCha web-search failed with HTTP {response.status_code}")
        try:
            data = response.json()
            code = int(data.get("code", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("BoCha web-search response has no valid body") from exc
        if code != 200:
            message = data.get("message") if isinstance(data, dict) else ""
            raise RuntimeError(f"BoCha web-search failed: code={code} message={message}")

        # 双键防御：真实响应结果在 data.webPages.value（Web Search 结构），
        # 也兼容 data.web_results（早期假设）。条目 name|title、url|link、snippet|content 差异。
        body = data.get("data") or {}
        web_pages = body.get("webPages") or {}
        web_results = (
            body.get("web_results")
            or web_pages.get("value")
            or []
        )
        results: list[SearchResult] = []
        for item in web_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            if not url:
                continue  # 无 url 的结果对 Agent 没用，直接丢
            results.append(
                SearchResult(
                    title=str(item.get("name") or item.get("title") or "").strip(),
                    url=url,
                    snippet=str(item.get("snippet") or item.get("content") or "").strip(),
                    summary=str(item.get("summary") or "").strip(),
                    platform="web",
                    published_at=str(
                        item.get("publish_date") or item.get("dateLastCrawled") or item.get("pubdate") or ""
                    ).strip(),
                )
            )
            if len(results) >= max_results:
                break
        return results


class BingRssSearchProvider:
    """无密钥的独立搜索源：读取 Bing 的 RSS 搜索结果。"""

    def __init__(
        self,
        *,
        base_url: str = BING_RSS_BASE_URL,
        timeout: float = BING_RSS_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        proxy: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.transport = transport
        self.proxy = proxy

    def search(
        self,
        query: str,
        *,
        max_results: int = MAX_RESULTS_DEFAULT,
        freshness: str = "noLimit",
    ) -> list[SearchResult]:
        proxy = self.proxy
        with httpx.Client(
            timeout=self.timeout,
            trust_env=False,
            transport=self.transport,
            proxy=proxy if self.transport is None else None,
            follow_redirects=True,
        ) as client:
            response = client.get(
                self.base_url,
                params={"format": "rss", "q": query},
                headers={"Accept": "application/rss+xml, application/xml, text/xml"},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Bing RSS search failed with HTTP {response.status_code}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError("Bing RSS search returned invalid XML") from exc
        results: list[SearchResult] = []
        for item in root.findall(".//item"):
            url = (item.findtext("link") or "").strip()
            if not url:
                continue
            results.append(SearchResult(
                title=(item.findtext("title") or "").strip(),
                url=url,
                snippet=(item.findtext("description") or "").strip(),
                platform="web:bing",
                published_at=(item.findtext("pubDate") or "").strip(),
            ))
            if len(results) >= max_results:
                break
        return results


class MultiSourceWebSearchProvider:
    """并行查询多个网页供应商，去重后按来源交替返回结果。"""

    def __init__(self, providers: Sequence[tuple[str, WebSearchProvider]]) -> None:
        self.providers = tuple(providers)
        self.last_failures: list[dict[str, str]] = []

    def search(
        self,
        query: str,
        *,
        max_results: int = MAX_RESULTS_DEFAULT,
        freshness: str = "noLimit",
    ) -> list[SearchResult]:
        if not self.providers:
            raise RuntimeError("no web search providers configured")
        self.last_failures = []
        grouped: list[list[SearchResult]] = [[] for _ in self.providers]
        successful = 0
        with ThreadPoolExecutor(max_workers=len(self.providers), thread_name_prefix="web-search") as pool:
            futures = {
                pool.submit(provider.search, query, max_results=max_results, freshness=freshness): index
                for index, (_name, provider) in enumerate(self.providers)
            }
            for future in as_completed(futures):
                index = futures[future]
                name = self.providers[index][0]
                try:
                    rows = future.result()
                except Exception as exc:
                    self.last_failures.append({"provider": name, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                successful += 1
                normalized: list[SearchResult] = []
                for row in rows or []:
                    platform = row.platform or "web"
                    if platform == "web":
                        platform = f"web:{name}"
                    normalized.append(SearchResult(
                        title=row.title,
                        url=row.url,
                        snippet=row.snippet,
                        summary=row.summary,
                        platform=platform,
                        published_at=row.published_at,
                    ))
                grouped[index] = normalized
        if not successful:
            details = "; ".join(item["provider"] + ": " + item["error"] for item in self.last_failures)
            raise RuntimeError(f"all web search providers failed{': ' + details if details else ''}")

        merged: list[SearchResult] = []
        seen: set[str] = set()
        position = 0
        while len(merged) < max_results and any(position < len(rows) for rows in grouped):
            for rows in grouped:
                if position >= len(rows):
                    continue
                row = rows[position]
                key = row.url.rstrip("/").lower()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(row)
                    if len(merged) >= max_results:
                        break
            position += 1
        return merged


# ---------------------------------------------------------------------------
# 3. schema + 校验（纯逻辑，无 HTTP）
# ---------------------------------------------------------------------------
WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索：普通问题直接搜，范围（平台、时间、地域）默认由你按问题自行决定，"
            "不要先向用户追问；只有复杂研究且范围会明显改变答案时才确认一次。"
            "返回搜索结果摘要、链接和覆盖说明；需要原文时再调用网页读取工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": SCHEMA_QUERY_LENGTH,
                    "description": (
                        "Use concise keywords, normally no more than 500 characters. "
                        "Do not paste the full task or output format; delegate long multi-source research."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": MAX_RESULTS_MIN,
                    "maximum": MAX_RESULTS_MAX,
                    "default": MAX_RESULTS_DEFAULT,
                },
                "freshness": {
                    "type": "string",
                    "enum": list(FRESHNESS_OPTIONS),
                    "default": "noLimit",
                },
                "scope": {
                    "type": "object",
                    "description": "已确认的研究范围；缺省时保持旧版单次网页搜索行为。",
                    "properties": {
                        "platforms": {"type": "array", "items": {"type": "string"}},
                        "time_range": {"type": "string"},
                        "region": {"type": "string"},
                        "source_types": {"type": "array", "items": {"type": "string"}},
                        "depth": {"type": "string", "enum": ["standard", "deep"]},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def _validated_args(arguments: dict[str, Any]) -> tuple[str, int, str]:
    """校验并归一化工具参数。非法输入抛 ValueError（由 handler 转成 failed 结果）。"""
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")
    query = query.strip()
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")

    max_results = arguments.get("max_results", MAX_RESULTS_DEFAULT)
    if not isinstance(max_results, bool) and isinstance(max_results, int):
        max_results = min(MAX_RESULTS_MAX, max(max_results, MAX_RESULTS_MIN))  # clamp 1..10
    else:
        raise ValueError("max_results must be an integer")

    freshness = arguments.get("freshness", "noLimit")
    if freshness not in FRESHNESS_OPTIONS:
        raise ValueError(f"freshness must be one of {FRESHNESS_OPTIONS}")

    return query, max_results, freshness


# ---------------------------------------------------------------------------
# 4. handler + 工厂
# ---------------------------------------------------------------------------
def _make_handler(provider: WebSearchProvider, router: AgentReachRouter | None = None) -> Any:
    def handle(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        # ① 校验失败 → failed（不要把异常漏给主体）。参数错误重试也不会好，归 permanent。
        raw_query = arguments.get("query")
        if isinstance(raw_query, str):
            arguments = {**arguments, "query": _compact_subagent_query(raw_query, scope)}
        try:
            query, max_results, freshness = _validated_args(arguments)
        except ValueError as exc:
            return {"status": "failed", "error": f"invalid arguments: {exc}", "error_kind": "permanent"}
        raw_scope = arguments.get("scope")
        research_scope = (
            ResearchScope.from_dict({"query": query, **raw_scope})
            if isinstance(raw_scope, dict)
            else ResearchScope(query=query, max_results=max_results)
        )
        effective_freshness = (
            research_scope.time_range
            if isinstance(raw_scope, dict)
            and "time_range" in raw_scope
            and research_scope.time_range in FRESHNESS_OPTIONS
            else freshness
        )
        # ② 网络/解析失败 → failed（LLM 会看到错误并自适应）。按异常类型分类，网络抖动归 transient。
        try:
            results = provider.search(query, max_results=max_results, freshness=effective_freshness)
        except Exception as exc:
            return {"status": "failed", "error": f"web_search provider error: {type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
        # ③ 成功 → {"status":"ok","output":{...}}（ToolExecutor 的归一化规则，勿加多余顶层键）
        requested = list(research_scope.platforms)
        active = [platform for platform in requested if platform == "web"]
        uncovered = [platform for platform in requested if platform not in active]
        # 渠道覆盖走**非阻塞**缓存查询：doctor 体检要 30 秒以上，绝不能
        # 内联等它；缓存未知时按"无覆盖信息"处理，后台会自行刷新。
        if router is not None:
            select_cached = getattr(router, "select_cached", None)
            cached = select_cached(tuple(requested)) if callable(select_cached) else router.select(tuple(requested))
            available = list(cached or ())
        else:
            available = []
        provider_failures = list(getattr(provider, "last_failures", []) or [])
        source_names = sorted({
            str(result.platform).split(":", 1)[1]
            for result in results
            if ":" in str(result.platform)
        })
        return {
            "status": "ok",
            "output": {
                "query": query,
                "backend": (
                    "multi-source"
                    if isinstance(provider, MultiSourceWebSearchProvider)
                    else "bocha"
                ),
                "sources": source_names,
                "provider_failures": provider_failures,
                "result_count": len(results),
                "scope": research_scope.to_dict(),
                "coverage": {
                    "requested_platforms": requested,
                    "active_platforms": active,
                    "available_platforms": available,
                    "uncovered_platforms": uncovered,
                    "time_range": research_scope.time_range,
                },
                "results": [
                    {
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "summary": result.summary,
                        "platform": result.platform,
                        "published_at": result.published_at,
                    }
                    for result in results
                ],
            },
        }

    return handle


# ── 意图门控配置 ──
# 网页搜索是"天气查询"和"资料搜索"两个意图的共同出口，所以挂两份配置。
# negative_terms 是这套配置存在的原因："天气局上班"和"查天气"字面几乎一样，
# 只靠对象词区分不开，必须显式排除。
SEARCH_QUERY_POLICY = IntentPolicy(
    intent_name="search_query",
    object_terms=("资料", "信息", "新闻", "价格", "多少钱", "最新", "用法", "成分"),
    required_slots=("query",),
)


def make_web_search_tool(
    provider: WebSearchProvider,
    *,
    risk: str = "low",
    timeout_seconds: float = DEFAULT_TIMEOUT,
    router: AgentReachRouter | None = None,
) -> ToolSpec:
    """用任意 provider 构造工具（测试/注入用）。默认 risk=low：自动搜索，不需确认。"""
    return ToolSpec(
        name="web_search",
        schema=WEB_SEARCH_SCHEMA,
        handler=_make_handler(provider, router),
        risk=risk,
        timeout_seconds=timeout_seconds,
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
        query_extractor="fact",
        intent_policies=(SEARCH_QUERY_POLICY,),
    )


def build_web_search_tool(provider: WebSearchProvider | None = None) -> ToolSpec | None:
    """即插即用入口：未注入 provider 时从 .env 构建 BoCha；未配置则返回 None（跳过注册）。"""
    if provider is None:
        provider = BoChaWebSearchProvider.from_env()
        if provider is not None and isinstance(provider, BoChaWebSearchProvider):
            enabled = os.environ.get("BING_RSS_ENABLED", "true").strip().lower() != "false"
            if enabled:
                provider = MultiSourceWebSearchProvider((
                    ("bocha", provider),
                    ("bing", BingRssSearchProvider()),
                ))
    if provider is None:
        return None
    return make_web_search_tool(provider, router=AgentReachRouter())
