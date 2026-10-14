"""兼容审计脚本自己的验收：它必须真的能发现问题，不能是个橡皮图章。

两边都验：干净库要判通过；植入了「同一事项两条现行版本」和「悬空引用」的库
必须被报出来。审计脚本只读，所以这里也让它在只读连接下跑。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import uuid
from pathlib import Path

from agent_core.memory_repositories import initialize_memory_repositories

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_memory_compatibility.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_memory_compatibility", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _insert_semantic_row(db_path: Path, *, record_id: str, semantics_key: str, **overrides) -> None:
    columns = {
        "id": record_id,
        "owner_id": "user",
        "channel": "web",
        "conversation_id": "c1",
        "module_id": "companion",
        "layer": "warm",
        "kind": "fact",
        "status": "active",
        "content": "预算三千",
        "metadata": "{}",
        "semantics_version": 1,
        "semantics_key": semantics_key,
        "scope_kind": "conversation",
        "record_version": 1,
        "review_state": "none",
        "created_at": "2026-10-11T10:00:00+00:00",
        "updated_at": "2026-10-11T10:00:00+00:00",
    }
    columns.update(overrides)
    placeholders = ",".join("?" for _ in columns)
    with sqlite3.connect(db_path) as db:
        db.execute(
            f"INSERT INTO memory_records({','.join(columns)}) VALUES({placeholders})",
            tuple(columns.values()),
        )


def test_a_clean_database_passes(tmp_path) -> None:
    module = _load_module()
    db_path = tmp_path / "records.db"
    repositories = initialize_memory_repositories(db_path, tmp_path / "vectors", vector_size=3)
    repositories.close()

    report = module.audit(db_path)

    assert report["ok"] is True, report["problems"]
    assert report["read_only"] is True
    assert report["facts"]["records"] == 0


def test_a_duplicated_current_version_is_reported(tmp_path) -> None:
    """同一事项两条现行版本 —— 读的时候用哪条说不清，必须在启用前被挡下。"""
    module = _load_module()
    db_path = tmp_path / "records.db"
    repositories = initialize_memory_repositories(db_path, tmp_path / "vectors", vector_size=3)
    repositories.close()
    key = "fact||budget"
    _insert_semantic_row(db_path, record_id=str(uuid.uuid4()), semantics_key=key)
    _insert_semantic_row(db_path, record_id=str(uuid.uuid4()), semantics_key=key)

    report = module.audit(db_path)

    assert report["ok"] is False
    assert any(item["kind"] == "duplicate_current_version" for item in report["problems"])


def test_a_dangling_effective_reference_is_reported(tmp_path) -> None:
    module = _load_module()
    db_path = tmp_path / "records.db"
    repositories = initialize_memory_repositories(db_path, tmp_path / "vectors", vector_size=3)
    repositories.close()
    _insert_semantic_row(
        db_path,
        record_id=str(uuid.uuid4()),
        semantics_key="fact||old",
        status="archived",
        content_status="superseded",
        effective_record_id=str(uuid.uuid4()),   # 指向一条不存在的记录
    )

    report = module.audit(db_path)

    assert report["ok"] is False
    assert any(item["kind"] == "dangling_effective_reference" for item in report["problems"])


def test_an_unknown_kind_status_pair_in_a_new_record_is_reported(tmp_path) -> None:
    module = _load_module()
    db_path = tmp_path / "records.db"
    repositories = initialize_memory_repositories(db_path, tmp_path / "vectors", vector_size=3)
    repositories.close()
    _insert_semantic_row(
        db_path,
        record_id=str(uuid.uuid4()),
        semantics_key="fact||weird",
        content_status="invented_status",
    )

    report = module.audit(db_path)

    assert report["ok"] is False
    assert any(item["kind"] == "unknown_kind_status_pair" for item in report["problems"])


def test_the_script_prints_json_and_returns_a_status_code(tmp_path) -> None:
    """能当上线前的一步跑：读得到 JSON，退出码能判。"""
    import subprocess

    db_path = tmp_path / "records.db"
    repositories = initialize_memory_repositories(db_path, tmp_path / "vectors", vector_size=3)
    repositories.close()

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["ok"] is True
