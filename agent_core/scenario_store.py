"""Private, single-scenario conversation ledger. No main-chat memory tables are used."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ScenarioStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scenario_sessions (
                    owner_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'drafting',
                    dialogue_mode TEXT NOT NULL DEFAULT 'roleplay',
                    scene_json TEXT NOT NULL DEFAULT '{}',
                    scene_prompt TEXT NOT NULL DEFAULT '',
                    scene_prompt_customized INTEGER NOT NULL DEFAULT 0,
                    style_prompt TEXT NOT NULL DEFAULT '',
                    persona_json TEXT NOT NULL DEFAULT '{}',
                    persona_reply_length TEXT NOT NULL DEFAULT 'balanced',
                    persona_initiative TEXT NOT NULL DEFAULT 'balanced',
                    persona_assistance TEXT NOT NULL DEFAULT 'balanced',
                    prompt_revision INTEGER NOT NULL DEFAULT 0,
                    summary_text TEXT NOT NULL DEFAULT '',
                    summary_upto_id INTEGER NOT NULL DEFAULT 0,
                    turn_status TEXT NOT NULL DEFAULT 'idle',
                    active_turn_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner_id, scenario_id)
                );
                CREATE TABLE IF NOT EXISTS scenario_turns (
                    owner_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    client_turn_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (owner_id, scenario_id, turn_id),
                    UNIQUE (owner_id, scenario_id, client_turn_id)
                );
                CREATE TABLE IF NOT EXISTS scenario_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'text',
                    content TEXT NOT NULL DEFAULT '',
                    sticker_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scenario_messages_session
                    ON scenario_messages(owner_id, scenario_id, message_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(scenario_sessions)")}
            if "summary_text" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN summary_text TEXT NOT NULL DEFAULT ''")
            if "summary_upto_id" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN summary_upto_id INTEGER NOT NULL DEFAULT 0")
            if "style_prompt" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN style_prompt TEXT NOT NULL DEFAULT ''")
            if "scene_prompt_customized" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN scene_prompt_customized INTEGER NOT NULL DEFAULT 0")
            if "prompt_revision" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN prompt_revision INTEGER NOT NULL DEFAULT 0")
            if "dialogue_mode" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN dialogue_mode TEXT NOT NULL DEFAULT 'roleplay'")
            if "persona_json" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN persona_json TEXT NOT NULL DEFAULT '{}'")
            if "persona_reply_length" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN persona_reply_length TEXT NOT NULL DEFAULT 'balanced'")
            if "persona_initiative" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN persona_initiative TEXT NOT NULL DEFAULT 'balanced'")
            if "persona_assistance" not in columns:
                db.execute("ALTER TABLE scenario_sessions ADD COLUMN persona_assistance TEXT NOT NULL DEFAULT 'balanced'")
            turn_columns = {row[1] for row in db.execute("PRAGMA table_info(scenario_turns)")}
            if "attachment_id" not in turn_columns:
                db.execute("ALTER TABLE scenario_turns ADD COLUMN attachment_id TEXT")
            message_columns = {row[1] for row in db.execute("PRAGMA table_info(scenario_messages)")}
            for column in ("attachment_id", "attachment_name", "attachment_media_type"):
                if column not in message_columns:
                    db.execute(f"ALTER TABLE scenario_messages ADD COLUMN {column} TEXT")
            db.execute("UPDATE scenario_turns SET status='interrupted' WHERE status='running'")
            db.execute("UPDATE scenario_sessions SET turn_status='interrupted' WHERE turn_status='running'")
            db.execute("UPDATE scenario_sessions SET phase='drafting' WHERE phase='collecting'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner_id: str) -> dict[str, Any]:
        scenario_id = f"scenario-{uuid.uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO scenario_sessions(owner_id,scenario_id,phase,created_at,updated_at) VALUES(?,?,'drafting',?,?)",
                (owner_id, scenario_id, now, now),
            )
        return self.snapshot(owner_id, scenario_id)  # type: ignore[return-value]

    def list(self, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT scenario_id,phase,dialogue_mode,scene_json,turn_status,updated_at FROM scenario_sessions "
                "WHERE owner_id=? ORDER BY updated_at DESC",
                (owner_id,),
            ).fetchall()
        return [
            {
                "scenario_id": row["scenario_id"], "phase": row["phase"],
                "dialogue_mode": row["dialogue_mode"],
                "scene_summary": json.loads(row["scene_json"]),
                "turn_status": row["turn_status"], "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def snapshot(self, owner_id: str, scenario_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
            if row is None:
                return None
            messages = db.execute(
                "SELECT message_id,turn_id,phase,role,kind,content,sticker_id,attachment_id,attachment_name,attachment_media_type,created_at "
                "FROM scenario_messages WHERE owner_id=? AND scenario_id=? ORDER BY message_id",
                (owner_id, scenario_id),
            ).fetchall()
        return {
            "scenario_id": scenario_id,
            "phase": row["phase"],
            "dialogue_mode": row["dialogue_mode"],
            "scene_summary": json.loads(row["scene_json"]),
            "scene_prompt_customized": bool(row["scene_prompt_customized"]),
            "turn_status": row["turn_status"],
            "active_turn_id": row["active_turn_id"],
            "messages": [dict(message) for message in messages],
            "updated_at": row["updated_at"],
        }

    def begin_turn(
        self, owner_id: str, scenario_id: str, text: str, client_turn_id: str,
        attachment_id: str | None = None, attachment_name: str | None = None,
        attachment_media_type: str | None = None,
    ) -> tuple[str, bool]:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute(
                "SELECT phase,turn_status FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
            if session is None:
                raise KeyError("scenario not found")
            existing = db.execute(
                "SELECT turn_id,text,attachment_id FROM scenario_turns WHERE owner_id=? AND scenario_id=? AND client_turn_id=?",
                (owner_id, scenario_id, client_turn_id),
            ).fetchone()
            if existing is not None:
                if existing["text"] != text or existing["attachment_id"] != attachment_id:
                    raise RuntimeError("client turn id reused with different content")
                return existing["turn_id"], False
            if session["turn_status"] == "running":
                raise RuntimeError("scenario turn already running")
            turn_id = f"turn-{uuid.uuid4().hex}"
            phase = session["phase"]
            message_phase = "dialogue" if phase == "dialogue" else "setup"
            db.execute(
                "INSERT INTO scenario_turns(owner_id,scenario_id,turn_id,client_turn_id,phase,text,status,created_at,attachment_id) "
                "VALUES(?,?,?,?,?,?,'running',?,?)",
                (owner_id, scenario_id, turn_id, client_turn_id, phase, text, now, attachment_id),
            )
            db.execute(
                "INSERT INTO scenario_messages(owner_id,scenario_id,turn_id,phase,role,content,created_at,attachment_id,attachment_name,attachment_media_type) "
                "VALUES(?,?,?,?, 'user',?,?,?,?,?)",
                (owner_id, scenario_id, turn_id, message_phase, text, now, attachment_id, attachment_name, attachment_media_type),
            )
            db.execute(
                "UPDATE scenario_sessions SET turn_status='running',active_turn_id=?,updated_at=? "
                "WHERE owner_id=? AND scenario_id=?",
                (turn_id, now, owner_id, scenario_id),
            )
        return turn_id, True

    def get_turn(self, owner_id: str, scenario_id: str, turn_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM scenario_turns WHERE owner_id=? AND scenario_id=? AND turn_id=?",
                (owner_id, scenario_id, turn_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def messages_for_phase(self, owner_id: str, scenario_id: str, phase: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT message_id,role,content,attachment_id,attachment_name,attachment_media_type FROM scenario_messages "
                "WHERE owner_id=? AND scenario_id=? AND phase=? AND kind='text' ORDER BY message_id",
                (owner_id, scenario_id, phase),
            ).fetchall()
        return [dict(row) for row in rows]

    def scene_prompt(self, owner_id: str, scenario_id: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT scene_prompt FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
        return str(row["scene_prompt"]) if row else ""

    def draft_scene(self, owner_id: str, scenario_id: str) -> dict[str, str]:
        with self._connect() as db:
            row = db.execute("SELECT scene_json FROM scenario_sessions WHERE owner_id=? AND scenario_id=?", (owner_id, scenario_id)).fetchone()
        return json.loads(row["scene_json"]) if row else {}

    def set_draft_scene(self, owner_id: str, scenario_id: str, scene: dict[str, str]) -> None:
        with self._connect() as db:
            db.execute("UPDATE scenario_sessions SET scene_json=?,updated_at=? WHERE owner_id=? AND scenario_id=? AND phase='collecting'",
                       (json.dumps(scene, ensure_ascii=False), time.time(), owner_id, scenario_id))

    def prompt_cards(self, owner_id: str, scenario_id: str, default_style: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT phase,scene_prompt,style_prompt,persona_json,persona_reply_length,"
                "persona_initiative,persona_assistance,prompt_revision FROM scenario_sessions "
                "WHERE owner_id=? AND scenario_id=?", (owner_id, scenario_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "scenario_id": scenario_id,
            "phase": row["phase"],
            "style_prompt": row["style_prompt"] or default_style,
            "scene_prompt": row["scene_prompt"],
            "persona": json.loads(row["persona_json"] or "{}"),
            "persona_preferences": {
                "reply_length": row["persona_reply_length"],
                "initiative": row["persona_initiative"],
                "assistance": row["persona_assistance"],
            },
            "revision": row["prompt_revision"],
        }

    def set_dialogue_mode(self, owner_id: str, scenario_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"roleplay", "chatbot"}:
            raise ValueError("invalid dialogue mode")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT phase,turn_status FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
            if row is None:
                raise KeyError("scenario not found")
            has_setup_message = db.execute(
                "SELECT 1 FROM scenario_messages WHERE owner_id=? AND scenario_id=? AND phase='setup' LIMIT 1",
                (owner_id, scenario_id),
            ).fetchone() is not None
            if row["turn_status"] == "running" or row["phase"] != "drafting" or has_setup_message:
                raise RuntimeError("dialogue mode cannot change after setup starts or during a turn")
            db.execute(
                "UPDATE scenario_sessions SET dialogue_mode=?,updated_at=? WHERE owner_id=? AND scenario_id=?",
                (mode, time.time(), owner_id, scenario_id),
            )
        return self.snapshot(owner_id, scenario_id)  # type: ignore[return-value]

    def set_prompt_cards(
        self, owner_id: str, scenario_id: str, style_prompt: str,
        scene_prompt: str, revision: int, *, persona: dict[str, str] | None = None,
        persona_preferences: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT phase,turn_status,prompt_revision,scene_prompt,scene_prompt_customized,"
                "persona_json,persona_reply_length,persona_initiative,persona_assistance FROM scenario_sessions "
                "WHERE owner_id=? AND scenario_id=?", (owner_id, scenario_id),
            ).fetchone()
            if row is None:
                raise KeyError("scenario not found")
            if row["turn_status"] == "running":
                raise RuntimeError("scenario turn is running")
            if row["prompt_revision"] != revision:
                raise RuntimeError("prompt cards changed; reload before saving")
            if row["phase"] in {"ready", "dialogue"} and not scene_prompt:
                raise ValueError("scene prompt cannot be empty")
            customized = int(bool(row["scene_prompt_customized"]) or row["scene_prompt"] != scene_prompt)
            if persona is None:
                persona = json.loads(row["persona_json"] or "{}")
            if persona_preferences is None:
                persona_preferences = {
                    "reply_length": row["persona_reply_length"],
                    "initiative": row["persona_initiative"],
                    "assistance": row["persona_assistance"],
                }
            db.execute(
                "UPDATE scenario_sessions SET style_prompt=?,scene_prompt=?,scene_prompt_customized=?,"
                "persona_json=?,persona_reply_length=?,persona_initiative=?,persona_assistance=?,"
                "prompt_revision=prompt_revision+1,updated_at=? "
                "WHERE owner_id=? AND scenario_id=?",
                (
                    style_prompt, scene_prompt, customized,
                    json.dumps(persona, ensure_ascii=False),
                    persona_preferences.get("reply_length", "balanced"),
                    persona_preferences.get("initiative", "balanced"),
                    persona_preferences.get("assistance", "balanced"),
                    time.time(), owner_id, scenario_id,
                ),
            )
        return {
            "scenario_id": scenario_id, "phase": row["phase"],
            "style_prompt": style_prompt, "scene_prompt": scene_prompt,
            "persona": persona, "persona_preferences": persona_preferences,
            "revision": revision + 1,
        }

    def summary_state(self, owner_id: str, scenario_id: str) -> tuple[str, int]:
        with self._connect() as db:
            row = db.execute(
                "SELECT summary_text,summary_upto_id FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
        return (str(row["summary_text"]), int(row["summary_upto_id"])) if row else ("", 0)

    def set_summary(self, owner_id: str, scenario_id: str, text: str, upto_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE scenario_sessions SET summary_text=?,summary_upto_id=? WHERE owner_id=? AND scenario_id=?",
                (text, upto_id, owner_id, scenario_id),
            )

    def set_scene(self, owner_id: str, scenario_id: str, scene: dict[str, str], prompt: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE scenario_sessions SET phase='dialogue',scene_json=?,scene_prompt=?,"
                "scene_prompt_customized=0,prompt_revision=prompt_revision+1,updated_at=? "
                "WHERE owner_id=? AND scenario_id=?",
                (json.dumps(scene, ensure_ascii=False), prompt, time.time(), owner_id, scenario_id),
            )

    def set_analysis(
        self, owner_id: str, scenario_id: str, scene: dict[str, str], prompt: str, *, ready: bool,
        persona: dict[str, str] | None = None,
        persona_preferences: dict[str, str] | None = None,
    ) -> None:
        phase = "ready" if ready and prompt.strip() else "drafting"
        with self._connect() as db:
            if persona is None or persona_preferences is None:
                db.execute(
                    "UPDATE scenario_sessions SET phase=?,scene_json=?,scene_prompt=?,"
                    "scene_prompt_customized=0,prompt_revision=prompt_revision+1,updated_at=? "
                    "WHERE owner_id=? AND scenario_id=? AND phase IN ('drafting','ready')",
                    (phase, json.dumps(scene, ensure_ascii=False), prompt, time.time(), owner_id, scenario_id),
                )
            else:
                db.execute(
                    "UPDATE scenario_sessions SET phase=?,scene_json=?,scene_prompt=?,"
                    "scene_prompt_customized=0,persona_json=?,persona_reply_length=?,persona_initiative=?,"
                    "persona_assistance=?,prompt_revision=prompt_revision+1,updated_at=? "
                    "WHERE owner_id=? AND scenario_id=? AND phase IN ('drafting','ready')",
                    (
                        phase, json.dumps(scene, ensure_ascii=False), prompt,
                        json.dumps(persona, ensure_ascii=False), persona_preferences["reply_length"],
                        persona_preferences["initiative"], persona_preferences["assistance"],
                        time.time(), owner_id, scenario_id,
                    ),
                )

    def start_dialogue(self, owner_id: str, scenario_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT phase,turn_status,scene_prompt FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
            if row is None:
                raise KeyError("scenario not found")
            if row["turn_status"] == "running":
                raise RuntimeError("scenario turn is running")
            if row["phase"] != "ready" or not str(row["scene_prompt"]).strip():
                raise RuntimeError("scenario card is not ready")
            db.execute(
                "UPDATE scenario_sessions SET phase='dialogue',updated_at=? WHERE owner_id=? AND scenario_id=?",
                (time.time(), owner_id, scenario_id),
            )
        return self.snapshot(owner_id, scenario_id)  # type: ignore[return-value]

    def append_assistant(
        self, owner_id: str, scenario_id: str, turn_id: str, phase: str,
        content: str, *, kind: str = "text", sticker_id: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO scenario_messages(owner_id,scenario_id,turn_id,phase,role,kind,content,sticker_id,created_at) "
                "VALUES(?,?,?,?, 'assistant',?,?,?,?)",
                (owner_id, scenario_id, turn_id, phase, kind, content, sticker_id, now),
            )
            db.execute(
                "UPDATE scenario_sessions SET updated_at=? WHERE owner_id=? AND scenario_id=?",
                (now, owner_id, scenario_id),
            )

    def finish_turn(self, owner_id: str, scenario_id: str, turn_id: str, status: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE scenario_turns SET status=? WHERE owner_id=? AND scenario_id=? AND turn_id=?",
                (status, owner_id, scenario_id, turn_id),
            )
            db.execute(
                "UPDATE scenario_sessions SET turn_status=?,updated_at=? WHERE owner_id=? AND scenario_id=?",
                (status, time.time(), owner_id, scenario_id),
            )

    def retry_turn(self, owner_id: str, scenario_id: str, turn_id: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute(
                "SELECT status FROM scenario_turns WHERE owner_id=? AND scenario_id=? AND turn_id=?",
                (owner_id, scenario_id, turn_id),
            ).fetchone()
            if turn is None or turn["status"] not in {"failed", "interrupted"}:
                return False
            session = db.execute(
                "SELECT turn_status FROM scenario_sessions WHERE owner_id=? AND scenario_id=?",
                (owner_id, scenario_id),
            ).fetchone()
            if session is None or session["turn_status"] == "running":
                return False
            db.execute(
                "DELETE FROM scenario_messages WHERE owner_id=? AND scenario_id=? AND turn_id=? AND role='assistant'",
                (owner_id, scenario_id, turn_id),
            )
            db.execute(
                "UPDATE scenario_turns SET status='running' WHERE owner_id=? AND scenario_id=? AND turn_id=?",
                (owner_id, scenario_id, turn_id),
            )
            db.execute(
                "UPDATE scenario_sessions SET turn_status='running',active_turn_id=?,updated_at=? "
                "WHERE owner_id=? AND scenario_id=?",
                (turn_id, time.time(), owner_id, scenario_id),
            )
        return True

    def sticker_history(self, owner_id: str, scenario_id: str) -> tuple[int, float | None]:
        with self._connect() as db:
            last = db.execute(
                "SELECT message_id,created_at FROM scenario_messages WHERE owner_id=? AND scenario_id=? "
                "AND kind='sticker' ORDER BY message_id DESC LIMIT 1",
                (owner_id, scenario_id),
            ).fetchone()
            after = int(last["message_id"]) if last else 0
            rows = db.execute(
                "SELECT COUNT(DISTINCT turn_id) FROM scenario_messages WHERE owner_id=? AND scenario_id=? "
                "AND role='assistant' AND phase='dialogue' AND kind='text' AND message_id>?",
                (owner_id, scenario_id, after),
            ).fetchone()
        return int(rows[0]), float(last["created_at"]) if last else None
