"""范围确认式联网研究的纯逻辑模型与解析器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from agent_core.contracts import Scope

SCOPE_TTL = timedelta(minutes=30)
KNOWN_PLATFORMS = (
    "web", "github", "rss", "youtube", "bilibili", "v2ex", "reddit", "twitter", "xiaohongshu"
)
TIME_RANGES = {"一天": "oneDay", "一周": "oneWeek", "一个月": "oneMonth", "一年": "oneYear"}


@dataclass(frozen=True)
class ResearchScope:
    query: str
    platforms: tuple[str, ...] = ("web",)
    time_range: str = "noLimit"
    region: str = ""
    source_types: tuple[str, ...] = ()
    depth: str = "standard"
    max_results: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "platforms": list(self.platforms),
            "time_range": self.time_range,
            "region": self.region,
            "source_types": list(self.source_types),
            "depth": self.depth,
            "max_results": self.max_results,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResearchScope":
        return cls(
            query=str(value.get("query") or "").strip(),
            platforms=tuple(str(item) for item in value.get("platforms", ("web",)) if str(item) in KNOWN_PLATFORMS) or ("web",),
            time_range=str(value.get("time_range") or "noLimit"),
            region=str(value.get("region") or "").strip(),
            source_types=tuple(str(item) for item in value.get("source_types", ())),
            depth=str(value.get("depth") or "standard"),
            max_results=max(1, min(10, int(value.get("max_results", 5)))),
        )


@dataclass(frozen=True)
class ResearchScopeDraft:
    scope: Scope
    query: str
    created_at: datetime
    suggested_platforms: tuple[str, ...] = ("web", "github", "rss")

    @property
    def expired(self) -> bool:
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created > SCOPE_TTL


class SQLiteResearchScopeStore:
    """持久化待确认范围，按完整对话 Scope 隔离。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS research_scope_drafts (
                    owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, module_id TEXT NOT NULL,
                    query TEXT NOT NULL, created_at TEXT NOT NULL,
                    suggested_platforms_json TEXT NOT NULL, PRIMARY KEY (
                        owner_id, channel, conversation_id, module_id
                    )
                )"""
            )
            db.commit()

    def save(self, draft: ResearchScopeDraft) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """INSERT INTO research_scope_drafts VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, channel, conversation_id, module_id)
                DO UPDATE SET query=excluded.query, created_at=excluded.created_at,
                suggested_platforms_json=excluded.suggested_platforms_json""",
                (
                    draft.scope.owner_id, draft.scope.channel, draft.scope.conversation_id,
                    draft.scope.module_id, draft.query, draft.created_at.isoformat(),
                    json.dumps(draft.suggested_platforms, ensure_ascii=False),
                ),
            )
            db.commit()

    def get(self, scope: Scope) -> ResearchScopeDraft | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT query, created_at, suggested_platforms_json FROM research_scope_drafts "
                "WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            ).fetchone()
        if row is None:
            return None
        try:
            created_at = datetime.fromisoformat(row[1])
            platforms = tuple(json.loads(row[2] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        draft = ResearchScopeDraft(scope, str(row[0]), created_at, platforms or ("web",))
        if draft.expired:
            self.clear(scope)
            return None
        return draft

    def clear(self, scope: Scope) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "DELETE FROM research_scope_drafts WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            )
            db.commit()


def scope_requires_confirmation(query: str) -> bool:
    """只在「重度研究且范围明显影响答案」时追问，普通问题一律直接查。

    旧版只要句子里出现"最新/比较/讨论"就先问一轮范围，日常问答被平白
    多拦一次。现在把触发词收紧到明确的调研动词：真正的大范围调研
    （平台/时间/地域会改变结论的）才值得停下来确认一次。
    """
    text = str(query or "").strip()
    if not text:
        return True
    if not any(word in text for word in ("调研", "研究", "综述", "深入", "全面梳理", "专题")):
        return False
    dimensions = 0
    if any(word in text.lower() for word in ("全网", "网页", "github", "社区", "论坛", "b站", "小红书", "reddit", "推特", "youtube")):
        dimensions += 1
    if any(word in text for word in ("今天", "最近", "过去", "本周", "本月", "今年", "一年", "一周", "一个月")):
        dimensions += 1
    if any(word in text for word in ("中文", "英文", "国内", "海外", "中国", "美国")):
        dimensions += 1
    if any(word in text for word in ("官方", "文档", "论文", "新闻", "用户评价", "讨论")):
        dimensions += 1
    return dimensions < 2


def build_scope_question(query: str) -> str:
    return (
        f"我先确认一下“{str(query).strip()}”的搜索范围：你希望看哪些平台（全网网页、技术/官方资料、中文社区或指定平台），"
        "时间范围和地域是否有限制？直接回复“按全网，最近一年”也可以？"
    )


def parse_scope_confirmation(text: str, draft: ResearchScopeDraft) -> ResearchScope | None:
    if draft.expired:
        return None
    value = str(text or "").strip().lower()
    if not value or value in {"取消", "算了", "不用了"}:
        return None
    platforms: list[str] = []
    if any(word in value for word in ("全网", "网页", "网上")):
        platforms.append("web")
    if any(word in value for word in ("技术", "官方", "github", "代码")):
        platforms.extend(("web", "github"))
    if any(word in value for word in ("rss", "订阅")):
        platforms.append("rss")
    if any(word in value for word in ("youtube", "油管")):
        platforms.append("youtube")
    if any(word in value for word in ("b站", "哔哩")):
        platforms.append("bilibili")
    if any(word in value for word in ("v2ex", "论坛")):
        platforms.append("v2ex")
    if any(word in value for word in ("reddit", "推特", "twitter", "小红书", "社区")):
        platforms.extend(("reddit", "twitter", "xiaohongshu"))
    if not platforms:
        return None
    time_range = "noLimit"
    for label, normalized in TIME_RANGES.items():
        if label in value:
            time_range = normalized
            break
    if "今天" in value or "过去24小时" in value:
        time_range = "oneDay"
    region = ""
    for label in ("中文", "中国", "国内", "海外", "美国", "欧洲"):
        if label in value:
            region = label
            break
    return ResearchScope(
        query=draft.query,
        platforms=tuple(dict.fromkeys(platforms)),
        time_range=time_range,
        region=region,
        source_types=("official",) if any(word in value for word in ("官方", "文档")) else (),
        depth="deep" if any(word in value for word in ("深入", "详细", "多来源")) else "standard",
    )


__all__ = ["ResearchScope", "ResearchScopeDraft", "SQLiteResearchScopeStore", "build_scope_question", "parse_scope_confirmation", "scope_requires_confirmation"]
