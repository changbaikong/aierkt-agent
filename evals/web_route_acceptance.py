"""Run the fixed phase-four cases through the actual asynchronous Web API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent_core.persistence import SQLiteRouteStore
from agent_core.project_env import load_project_env
from agent_core.work_ledger import WorkLedger
from evals.route_observation_acceptance import campaign_status
from evals.web_route_acceptance_cases import WEB_CASES


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_fixtures(base: Path, campaign_id: str, *, config_dir: Path | None = None) -> dict[str, Any]:
    root = base.resolve() / campaign_id
    projects = []
    for case in WEB_CASES:
        if case.fixture is None:
            continue
        project = root / "projects" / case.fixture.name
        project.mkdir(parents=True, exist_ok=True)
        source, tests = project / "app.py", project / "test_app.py"
        for path, content in ((source, case.fixture.source), (tests, case.fixture.tests)):
            if path.exists() and path.read_text(encoding="utf-8") != content:
                raise RuntimeError(f"fixture was modified: {path}")
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        projects.append({
            "case_id": case.case_id, "project_root": str(project),
            "original_hashes": {"app.py": _hash(source), "test_app.py": _hash(tests)},
        })
    host_dir = config_dir or Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "ArcueidAgent" / "acceptance-config"
    host_dir.mkdir(parents=True, exist_ok=True)
    config_path = host_dir / f"{campaign_id}-verification.json"
    config = {"version": 1, "projects": [
        {"project_root": item["project_root"], "commands": [
            {"runtime": "python", "args": ["-B", "-c",
                "import hashlib,pathlib,runpy; p=pathlib.Path('test_app.py'); "
                f"assert hashlib.sha256(p.read_bytes()).hexdigest()=={item['original_hashes']['test_app.py']!r}; "
                "runpy.run_path(str(p),run_name='__main__')"],
             "cwd": ".", "timeout_seconds": 60}
        ]}
        for item in projects
    ]}
    content = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if config_path.exists() and config_path.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"verification config was modified: {config_path}")
    config_path.write_text(content, encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = {
        "campaign_id": campaign_id, "projects": projects,
        "verification_config": str(config_path), "manifest_path": str(manifest_path),
    }
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise RuntimeError(f"fixture manifest was modified: {manifest_path}")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _request(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    with urlopen(Request(url, data=data, method=method,
                         headers={"Content-Type": "application/json"}), timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Web API returned a non-object response")
    return result


def _save_accepted(store: SQLiteRouteStore, case_id: str, value: dict[str, str]) -> None:
    state = store.observation_state()
    snapshot = dict(state["snapshot"])
    accepted = dict(snapshot.get("accepted_cases") or {})
    accepted[case_id] = value
    snapshot["accepted_cases"] = accepted
    store.save_observation_state(snapshot=snapshot, phase=state["phase"])


def _originals_intact(manifest: dict[str, Any]) -> bool:
    return all(
        (Path(item["project_root"]) / name).is_file()
        and _hash(Path(item["project_root"]) / name) == digest
        for item in manifest["projects"] for name, digest in item["original_hashes"].items()
    )


def run_campaign(
    store: SQLiteRouteStore,
    manifest: dict[str, Any],
    *,
    base_url: str,
    request: Callable[[str, str, dict[str, Any] | None], dict[str, Any]] = _request,
    poll_interval: float = 2,
    timeout: float = 600,
    max_new_cases: int | None = None,
    work_ledger: WorkLedger | None = None,
) -> dict[str, Any]:
    state = store.observation_state()
    snapshot = dict(state["snapshot"])
    campaign_id = str(snapshot.get("campaign_id") or "")
    if not campaign_id or campaign_id != manifest.get("campaign_id"):
        raise RuntimeError("campaign and fixture manifest do not match")
    if str(snapshot.get("fixture_manifest_path")) != str(manifest.get("manifest_path")):
        raise RuntimeError("campaign fixture path does not match")
    if not _originals_intact(manifest):
        raise RuntimeError("original fixture files have changed")
    if state["phase"] in {"failed", "passed", "manual_review_ready", "manual_review_in_progress"}:
        return {"campaign_id": campaign_id, "submitted": [], "reason": state["phase"], "phase": state["phase"]}
    base = base_url.rstrip("/")
    submitted: list[str] = []
    project_by_case = {item["case_id"]: item for item in manifest["projects"]}

    def stop(reason: str) -> dict[str, Any]:
        current = store.observation_state()
        snap = {**current["snapshot"], "abnormal": reason}
        store.save_observation_state(snapshot=snap, phase="blocked")
        return {"campaign_id": campaign_id, "submitted": submitted, "reason": reason, "phase": "blocked"}

    for case in WEB_CASES:
        if max_new_cases is not None and len(submitted) >= max_new_cases:
            break
        try:
            health = request("GET", base + "/api/health", None)
            if health.get("status") not in {"healthy", "degraded"}:
                return stop("service_unavailable")
            current = store.observation_state()
            if current["phase"] == "blocked":
                store.save_observation_state(snapshot={**current["snapshot"], "abnormal": ""}, phase="observing")
            history_url = (base + "/api/conversations/" + quote(
                f"{snapshot['conversation_prefix']}{case.case_id.lower()}", safe="")
                + "/history?account_id=" + quote(snapshot["account_id"], safe=""))
            accepted = dict((store.observation_state()["snapshot"].get("accepted_cases") or {}).get(case.case_id) or {})
            if accepted.get("state") == "submitting":
                # A lost HTTP response may still have been accepted. Never guess and resend.
                history = request("GET", history_url, None)
                if not history.get("messages"):
                    return stop("submission_outcome_unknown")
                _save_accepted(store, case.case_id, {"state": "accepted", "turn_id": "response_lost"})
                accepted = {"state": "accepted"}
            if not accepted:
                prompt = case.prompt.format(project_root=project_by_case[case.case_id]["project_root"]) if case.fixture else case.prompt
                _save_accepted(store, case.case_id, {"state": "submitting", "turn_id": ""})
                reply = request("POST", base + "/api/chat", {
                    "message": prompt, "account_id": snapshot["account_id"],
                    "conversation_id": f"{snapshot['conversation_prefix']}{case.case_id.lower()}",
                })
                if reply.get("status") != "accepted" or not reply.get("turn_id"):
                    return stop("submission_outcome_unknown")
                _save_accepted(store, case.case_id, {"state": "accepted", "turn_id": str(reply["turn_id"])})
                submitted.append(case.case_id)
            deadline = time.monotonic() + timeout
            while True:
                history = request("GET", history_url, None)
                has_reply = any(item.get("role") == "assistant" and item.get("content")
                                for item in history.get("messages") or [])
                status = campaign_status(store, work_ledger=work_ledger)
                row = next((item for item in status.get("records", []) if item["case_id"] == case.case_id), None)
                if status["phase"] == "failed":
                    return stop(status.get("abnormal") or "campaign_failed")
                terminal_failure_reply = any(
                    item.get("role") == "assistant"
                    and str(item.get("content") or "").strip() == "回复生成失败，请稍后重试。"
                    for item in history.get("messages") or []
                )
                if terminal_failure_reply and row is None:
                    return stop("route_record_missing")
                if has_reply and row and row.get("final_status"):
                    if case.fixture is None or status["task_evidence"].get(str(row.get("task_id") or ""), {}).get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                        break
                if timeout <= 0:
                    if has_reply:
                        break
                    return {"campaign_id": campaign_id, "submitted": submitted, "reason": "pending", "phase": status["phase"]}
                if time.monotonic() >= deadline:
                    return stop("turn_timeout")
                time.sleep(poll_interval)
            if not _originals_intact(manifest):
                return stop("original_project_modified")
        except Exception as exc:
            return stop(f"service_interrupted:{type(exc).__name__}")
    status = campaign_status(store, work_ledger=work_ledger)
    return {"campaign_id": campaign_id, "submitted": submitted,
            "reason": "complete" if status.get("gate_reached") else "pending",
            "phase": status["phase"]}


def main() -> None:
    load_project_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--base", type=Path, default=Path(".pytmp/web-acceptance"))
    parser.add_argument("--db", type=Path, default=Path(os.getenv("AGENT_CORE_DB", "agent_core.db")))
    parser.add_argument("--work-db", type=Path, default=Path(os.getenv("AGENT_WORK_DB", "agent_work.db")))
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_fixtures(args.base, args.campaign_id)
    else:
        path = args.base.resolve() / args.campaign_id / "manifest.json"
        result = run_campaign(SQLiteRouteStore(args.db), json.loads(path.read_text(encoding="utf-8")),
                              base_url=args.url, work_ledger=WorkLedger(args.work_db))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
