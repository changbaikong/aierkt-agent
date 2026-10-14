"""Async post-processing for Web memory extraction turns."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .contracts import Scope
from .memory import MemoryContext
from .memory_extract import ExtractedMemory, MemoryExtractor
from .memory_logging import configure_memory_logging
from .memory_writes import MemoryWriter


LOGGER = logging.getLogger("agent_core.memory.postprocess")

configure_memory_logging()


def _result_summary(extracted: list[ExtractedMemory], persisted: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "stored" if persisted else "empty",
        "extracted": len(extracted),
        "persisted": len(persisted),
        "persisted_ids": [record["id"] for record in persisted],
    }


async def extract_and_store_memory_turn(
    extractor: MemoryExtractor | None,
    writer: MemoryWriter | None,
    scope: Scope,
    user_text: str,
    assistant_reply: str = "",
    *,
    history: list[dict[str, Any]] | None = None,
    turn_id: str = "",
    run_id: str = "",
    occurred_at: str = "",
) -> dict[str, Any]:
    """Run extraction and persist the result through the normal writer path.

    ``occurred_at`` 是这轮对话**实际发生**的时间。云端补提炼历史对话时会传它，
    记忆寿命从那会儿算起；不传时退回写入时刻（Web 上的实时对话就是这种情况）。
    """
    if extractor is None or writer is None:
        return {"status": "skipped", "reason": "memory_extraction_not_configured", "extracted": 0, "persisted": 0}

    try:
        extracted = await extractor.extract(user_text, assistant_reply=assistant_reply, history=history)
        if not extracted:
            LOGGER.info(
                "Web 记忆提炼为空：turn=%s run=%s scope=%s/%s/%s",
                turn_id,
                run_id,
                scope.owner_id,
                scope.channel,
                scope.conversation_id,
            )
            return {"status": "empty", "extracted": 0, "persisted": 0}

        persisted = await asyncio.to_thread(
            writer.save_extracted_memories,
            scope,
            extracted,
            user_text=user_text,
            assistant_reply=assistant_reply,
            turn_id=turn_id,
            run_id=run_id,
            occurred_at=occurred_at,
        )
        result = _result_summary(extracted, persisted)
        LOGGER.info(
            "Web 记忆提炼完成：turn=%s run=%s extracted=%d persisted=%d",
            turn_id,
            run_id,
            result["extracted"],
            result["persisted"],
        )
        return result
    except Exception as exc:
        LOGGER.warning(
            "Web 记忆提炼失败：turn=%s run=%s error=%s: %s",
            turn_id,
            run_id,
            type(exc).__name__,
            exc,
        )
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "extracted": 0,
            "persisted": 0,
        }
