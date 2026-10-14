import json
from collections import Counter
from pathlib import Path

from agent_core.persistence import SQLiteRouteStore
from evals.web_route_acceptance_cases import WEB_CASES
from evals.web_route_acceptance import prepare_fixtures, run_campaign


def test_manifest_and_host_verification(tmp_path: Path) -> None:
    assert len(WEB_CASES) == len({case.case_id for case in WEB_CASES}) == 30
    assert Counter(case.category for case in WEB_CASES) == {
        "direct": 8, "network": 4, "subagent": 3, "readonly_code": 5,
        "missing_project_root": 5, "code_agent": 5,
    }
    manifest = prepare_fixtures(tmp_path, "batch-1", config_dir=tmp_path / "config")
    config = json.loads(Path(manifest["verification_config"]).read_text(encoding="utf-8"))
    assert len(config["projects"]) == 5
    for item in manifest["projects"]:
        root = Path(item["project_root"])
        assert root.joinpath("app.py").is_file()
        assert root.joinpath("test_app.py").is_file()
        assert item["original_hashes"]["app.py"]
        assert item["original_hashes"]["test_app.py"]
    assert all(project["commands"][0]["args"][:2] == ["-B", "-c"] for project in config["projects"])
    assert all("sha256" in project["commands"][0]["args"][2] for project in config["projects"])


def test_accepted_request_is_never_resent_after_interruption(tmp_path: Path) -> None:
    store = SQLiteRouteStore(tmp_path / "routes.db")
    manifest = prepare_fixtures(tmp_path, "batch-1", config_dir=tmp_path / "config")
    snapshot = {
        "campaign_id": "batch-1", "account_id": "route-acceptance",
        "conversation_prefix": "r4-batch-1-", "started_at": "2026-09-28T10:00:00",
        "fixture_manifest_path": manifest["manifest_path"],
        "case_ids": [case.case_id for case in WEB_CASES],
        "expected_routes": {case.case_id: case.expected_route for case in WEB_CASES},
        "accepted_cases": {WEB_CASES[0].case_id: {"state": "accepted", "turn_id": "turn-1"}},
    }
    store.begin_observation_campaign(snapshot)
    calls = []

    def request(method, url, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/api/health"):
            return {"status": "healthy"}
        if method == "GET" and "/history?" in url:
            return {"messages": [{"role": "assistant", "content": "done"}]}
        if method == "POST":
            return {"status": "accepted", "turn_id": "turn-2"}
        raise AssertionError(url)

    result = run_campaign(store, manifest, base_url="http://127.0.0.1:8765", request=request,
                          poll_interval=0, timeout=0, max_new_cases=1)
    assert result["submitted"] == [WEB_CASES[1].case_id]
    assert len([call for call in calls if call[0] == "POST"]) == 1
    assert store.observation_state()["snapshot"]["accepted_cases"][WEB_CASES[1].case_id]["turn_id"] == "turn-2"


def test_uncertain_submission_blocks_without_resending(tmp_path: Path) -> None:
    store = SQLiteRouteStore(tmp_path / "routes.db")
    manifest = prepare_fixtures(tmp_path, "batch-2", config_dir=tmp_path / "config")
    store.begin_observation_campaign({
        "campaign_id": "batch-2", "account_id": "route-acceptance",
        "conversation_prefix": "r4-batch-2-", "started_at": "2026-09-28T10:00:00",
        "fixture_manifest_path": manifest["manifest_path"],
        "case_ids": [case.case_id for case in WEB_CASES],
        "expected_routes": {case.case_id: case.expected_route for case in WEB_CASES},
        "accepted_cases": {WEB_CASES[0].case_id: {"state": "submitting"}},
    })
    calls = []

    def request(method, url, payload=None):
        calls.append(method)
        return {"status": "healthy"} if url.endswith("/api/health") else {"messages": []}

    result = run_campaign(store, manifest, base_url="http://127.0.0.1:8765", request=request,
                          poll_interval=0, timeout=0)
    assert result["reason"] == "submission_outcome_unknown"
    assert "POST" not in calls


def test_final_reply_without_route_record_stops_instead_of_polling_forever(tmp_path: Path) -> None:
    store = SQLiteRouteStore(tmp_path / "routes.db")
    manifest = prepare_fixtures(tmp_path, "batch-3", config_dir=tmp_path / "config")
    store.begin_observation_campaign({
        "campaign_id": "batch-3", "account_id": "route-acceptance",
        "conversation_prefix": "r4-batch-3-", "started_at": "2026-09-28T10:00:00",
        "fixture_manifest_path": manifest["manifest_path"],
        "case_ids": [case.case_id for case in WEB_CASES],
        "expected_routes": {case.case_id: case.expected_route for case in WEB_CASES},
        "accepted_cases": {},
    })

    def request(method, url, payload=None):
        if method == "GET" and url.endswith("/api/health"):
            return {"status": "healthy"}
        if method == "POST":
            return {"status": "accepted", "turn_id": "turn-1"}
        return {"messages": [{"role": "assistant", "content": "回复生成失败，请稍后重试。"}]}

    result = run_campaign(
        store, manifest, base_url="http://127.0.0.1:8765", request=request,
        poll_interval=0, timeout=1, max_new_cases=1,
    )
    assert result["reason"] == "route_record_missing"
