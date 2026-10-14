"""Provider-neutral, read-only NetEase music catalog tool contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.tools import classify_tool_error


MAX_QUERY_LENGTH = 200
DEFAULT_LIMIT = 5
MAX_LIMIT = 10


@dataclass(frozen=True)
class Track:
    track_id: str
    title: str
    artist: str
    album: str = ""


class NeteaseCatalogProvider(Protocol):
    def search_tracks(self, query: str, *, limit: int) -> list[Track]: ...


NETEASE_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tracks",
        "description": "Search the configured NetEase music catalog. This operation is read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": MAX_QUERY_LENGTH},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def _validated_arguments(arguments: dict) -> tuple[str, int]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")
    query = query.strip()
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    return query, min(MAX_LIMIT, max(1, limit))


def make_netease_search_tool(provider: NeteaseCatalogProvider) -> ToolSpec:
    def handler(arguments: dict, _scope: Scope) -> dict:
        try:
            query, limit = _validated_arguments(arguments)
            tracks = provider.search_tracks(query, limit=limit)
        except ValueError as exc:
            return {"status": "failed", "error": f"invalid arguments: {exc}", "error_kind": "permanent"}
        except Exception as exc:
            return {"status": "failed", "error": f"netease catalog provider error: {type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
        return {
            "status": "ok",
            "output": {
                "query": query,
                "tracks": [
                    {"id": item.track_id, "title": item.title, "artist": item.artist, "album": item.album}
                    for item in tracks
                ],
            },
        }

    return ToolSpec("search_tracks", NETEASE_SEARCH_SCHEMA, handler, risk="low", effect=EFFECT_READ, deterministic_route_allowed=True, query_extractor="music")
