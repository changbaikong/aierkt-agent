"""HTTP boundary for the independent Scenario Runtime."""

from __future__ import annotations

import mimetypes
from typing import Callable, Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .scenario_runtime import ScenarioRuntime
from .validation import (
    MAX_CONVERSATION_ID_LENGTH,
    MAX_OWNER_ID_LENGTH,
    ValidationError as InputValidationError,
    validate_field_length,
    validate_safe_id,
)


def _safe_id(value: str, name: str, maximum: int) -> str:
    try:
        validate_field_length(value, maximum, name)
        validate_safe_id(value, name)
    except InputValidationError as exc:
        raise ValueError(exc.message) from exc
    return value


class ScenarioCreateRequest(BaseModel):
    account_id: str = "local-user"

    @field_validator("account_id")
    @classmethod
    def account_is_safe(cls, value: str) -> str:
        return _safe_id(value, "account_id", MAX_OWNER_ID_LENGTH)


class ScenarioMessageRequest(ScenarioCreateRequest):
    text: str = Field(default="", max_length=2000)
    attachment_id: str | None = Field(default=None, max_length=96)
    client_turn_id: str = Field(min_length=1, max_length=96)

    @field_validator("client_turn_id")
    @classmethod
    def turn_id_is_safe(cls, value: str) -> str:
        return _safe_id(value, "client_turn_id", 96)

    @field_validator("attachment_id")
    @classmethod
    def attachment_id_is_safe(cls, value: str | None) -> str | None:
        return _safe_id(value, "attachment_id", 96) if value is not None else None


class ScenarioPromptCardsRequest(ScenarioCreateRequest):
    revision: int = Field(ge=0)
    style_prompt: str = Field(max_length=4000)
    scene_prompt: str = Field(max_length=6000)
    persona: dict[str, str] = Field(default_factory=dict)
    persona_preferences: dict[str, str] = Field(default_factory=dict)


class ScenarioPersonaAnalysisRequest(ScenarioCreateRequest):
    source_text: str = Field(min_length=1, max_length=12000)

    @field_validator("source_text")
    @classmethod
    def source_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("persona source cannot be blank")
        return value


class ScenarioModeRequest(ScenarioCreateRequest):
    dialogue_mode: Literal["roleplay", "chatbot"]


def _query_id(value: str, name: str, maximum: int) -> str:
    try:
        return _safe_id(value, name, maximum)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def make_scenario_router(runtime_factory: Callable[[], ScenarioRuntime]) -> APIRouter:
    router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

    @router.post("", status_code=201)
    def create_scenario(request: ScenarioCreateRequest):
        return runtime_factory().create(request.account_id)

    @router.get("")
    def list_scenarios(account_id: str = "local-user"):
        owner = _query_id(account_id, "account_id", MAX_OWNER_ID_LENGTH)
        return {"scenarios": runtime_factory().list(owner)}

    @router.get("/stickers/{sticker_id}")
    def get_sticker(sticker_id: str):
        path = runtime_factory().stickers.resolve(sticker_id)
        if path is None:
            raise HTTPException(status_code=404, detail="sticker not found")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @router.get("/{scenario_id}")
    def get_scenario(scenario_id: str, account_id: str = "local-user"):
        owner = _query_id(account_id, "account_id", MAX_OWNER_ID_LENGTH)
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        snapshot = runtime_factory().snapshot(owner, identifier)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="scenario not found")
        return snapshot

    @router.post("/{scenario_id}/attachments", status_code=201)
    async def upload_attachment(
        scenario_id: str, file: UploadFile = File(...), account_id: str = Form("local-user"),
    ):
        owner = _query_id(account_id, "account_id", MAX_OWNER_ID_LENGTH)
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        runtime = runtime_factory()
        content = await file.read(runtime.attachment_store.max_bytes + 1)
        try:
            return runtime.upload_attachment(owner, identifier, file.filename or "", file.content_type or "", content)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=413 if len(content) > runtime.attachment_store.max_bytes else 422, detail=str(exc)) from exc

    @router.get("/{scenario_id}/attachments/{attachment_id}")
    def get_attachment(scenario_id: str, attachment_id: str, account_id: str = "local-user"):
        owner = _query_id(account_id, "account_id", MAX_OWNER_ID_LENGTH)
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        safe_attachment = _query_id(attachment_id, "attachment_id", 96)
        item = runtime_factory().attachment_store.get(owner, identifier, safe_attachment)
        if item is None or item.kind != "image":
            raise HTTPException(status_code=404, detail="image attachment not found")
        return FileResponse(item.path, media_type=item.media_type)

    @router.get("/{scenario_id}/prompt-cards")
    def get_prompt_cards(scenario_id: str, account_id: str = "local-user"):
        owner = _query_id(account_id, "account_id", MAX_OWNER_ID_LENGTH)
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        cards = runtime_factory().prompt_cards(owner, identifier)
        if cards is None:
            raise HTTPException(status_code=404, detail="scenario not found")
        return cards

    @router.put("/{scenario_id}/prompt-cards")
    def put_prompt_cards(scenario_id: str, request: ScenarioPromptCardsRequest):
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        try:
            return runtime_factory().update_prompt_cards(
                request.account_id, identifier, request.style_prompt,
                request.scene_prompt, request.revision, persona=request.persona,
                persona_preferences=request.persona_preferences,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{scenario_id}/persona/analyze")
    async def analyze_persona(scenario_id: str, request: ScenarioPersonaAnalysisRequest):
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        try:
            return await runtime_factory().analyze_persona(request.account_id, identifier, request.source_text)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{scenario_id}/messages", status_code=202)
    def send_message(scenario_id: str, request: ScenarioMessageRequest, background: BackgroundTasks):
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        runtime = runtime_factory()
        try:
            turn_id, fresh = runtime.accept_message(
                request.account_id, identifier, request.text, request.client_turn_id, request.attachment_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if fresh:
            background.add_task(runtime.run_turn, request.account_id, identifier, turn_id)
        return {"turn_id": turn_id, "status": "accepted", "fresh": fresh}

    @router.post("/{scenario_id}/start")
    def start_scenario(scenario_id: str, request: ScenarioCreateRequest):
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        try:
            return runtime_factory().start_dialogue(request.account_id, identifier)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.put("/{scenario_id}/mode")
    def set_scenario_mode(scenario_id: str, request: ScenarioModeRequest):
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        try:
            return runtime_factory().set_dialogue_mode(request.account_id, identifier, request.dialogue_mode)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{scenario_id}/turns/{turn_id}/retry", status_code=202)
    def retry_turn(scenario_id: str, turn_id: str, request: ScenarioCreateRequest, background: BackgroundTasks):
        identifier = _query_id(scenario_id, "scenario_id", MAX_CONVERSATION_ID_LENGTH)
        safe_turn = _query_id(turn_id, "turn_id", MAX_CONVERSATION_ID_LENGTH)
        runtime = runtime_factory()
        if not runtime.retry(request.account_id, identifier, safe_turn):
            raise HTTPException(status_code=409, detail="turn cannot be retried")
        background.add_task(runtime.run_turn, request.account_id, identifier, safe_turn)
        return {"turn_id": safe_turn, "status": "accepted"}

    return router
