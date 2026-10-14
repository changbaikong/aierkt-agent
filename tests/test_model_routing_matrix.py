from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from agent_core.contracts import Scope
from evals.model_routing_matrix import (
    ALLOWED_ROUTE_KINDS,
    CASES,
    DEFAULT_REPEATS,
    build_registry,
    summarize,
)


def test_model_routing_matrix_has_sixty_unique_cases_and_three_repeats() -> None:
    assert len(CASES) >= 60
    assert len({case.case_id for case in CASES}) == len(CASES)
    assert DEFAULT_REPEATS == 3
    assert {case.expected_route for case in CASES} <= ALLOWED_ROUTE_KINDS


def test_model_routing_matrix_covers_required_paths_and_code_agent_depth() -> None:
    categories = {case.category for case in CASES}
    assert {
        "direct",
        "specific_tool",
        "network",
        "subagent",
        "code_agent",
        "missing_project_root",
        "readonly_code_question",
        "plan",
        "failure_or_rejection",
    } <= categories
    assert sum(case.expected_route == "code_agent" for case in CASES) >= 10
    assert all(
        case.expected_route == "direct"
        for case in CASES
        if case.category == "missing_project_root"
    )


def test_plan_cases_have_available_screen_capture_and_project_root() -> None:
    registry = build_registry([])
    submit = registry.resolve("work__submit")
    assert submit is not None
    task_types = submit.schema["function"]["parameters"]["properties"]["task_type"]["enum"]
    assert "screen.capture" in task_types
    plan = registry.resolve("work__plan_submit")
    assert plan is not None
    steps = plan.schema["function"]["parameters"]["properties"]["steps"]
    assert "task_type" in steps["items"]["properties"]
    assert all("{project_root}" in case.prompt for case in CASES if case.category == "plan")


def test_model_routing_summary_enforces_quality_and_safety_thresholds() -> None:
    results = [
        {
            "case_id": "direct-1",
            "expected_route": "direct",
            "actual_route": "direct",
            "must_not_persist": True,
            "approval_bypassed": False,
        },
        {
            "case_id": "code-1",
            "expected_route": "code_agent",
            "actual_route": "code_agent",
            "must_not_persist": False,
            "approval_bypassed": False,
        },
    ]

    summary = summarize(
        results,
        expected_case_count=2,
        required_repeats=1,
        model_name="test-model",
        generated_at=datetime(2026, 9, 28, 10, 0, 0),
    )

    assert summary["overall_accuracy"] == 1.0
    assert summary["code_agent_recall"] == 1.0
    assert summary["persistent_false_positives"] == 0
    assert summary["approval_bypasses"] == 0
    assert summary["case_count"] == 2
    assert summary["repeats"] == 1
    assert summary["complete"] is True
    assert summary["model"] == "test-model"
    assert summary["generated_at"] == "2026-09-28T10:00:00"
    assert summary["passed"] is True


def test_model_routing_summary_rejects_reduced_or_incomplete_matrix() -> None:
    results = [{
        "case_id": "direct-1",
        "repeat": 1,
        "expected_route": "direct",
        "actual_route": "direct",
    }]

    summary = summarize(
        results,
        expected_case_count=len(CASES),
        required_repeats=DEFAULT_REPEATS,
        model_name="test-model",
    )

    assert summary["complete"] is False
    assert summary["passed"] is False


def test_subagent_followed_by_search_counts_as_valid_multi_source_route() -> None:
    result = summarize([{
        "expected_route": "subagent",
        "actual_route": "mixed",
        "requested_tools": ["subagent__dispatch", "search__web"],
        "case_id": "subagent-1",
        "repeat": 1,
    }], expected_case_count=1, required_repeats=1)
    assert result["correct"] == 1


def test_repeated_readonly_fallback_after_subagent_still_counts_as_subagent_route() -> None:
    result = summarize([{
        "expected_route": "subagent",
        "actual_route": "mixed",
        "requested_tools": ["subagent__dispatch", "search__web", "search__web"],
        "case_id": "subagent-1",
        "repeat": 1,
    }], expected_case_count=1, required_repeats=1)
    assert result["correct"] == 1


def test_search_first_or_background_work_does_not_count_as_subagent_route() -> None:
    results = [
        {"case_id": "subagent-1", "repeat": 1, "expected_route": "subagent", "actual_route": "mixed", "requested_tools": ["search__web", "subagent__dispatch"]},
        {"case_id": "subagent-2", "repeat": 1, "expected_route": "subagent", "actual_route": "mixed", "requested_tools": ["subagent__dispatch", "work__submit", "search__web"]},
    ]
    assert summarize(results, expected_case_count=2, required_repeats=1)["correct"] == 0


def test_missing_delete_identifier_requires_clarification_not_tool_call() -> None:
    missing = [case for case in CASES if case.category == "missing_delete_identifier"]
    assert len(missing) == 2
    assert all(case.expected_route == "direct" and case.must_not_persist for case in missing)


def test_failure_cases_request_an_actual_lookup_not_a_prior_rejection() -> None:
    failures = [case for case in CASES if case.category == "failure_or_rejection"]
    assert all("故障查询" in case.prompt for case in failures)
    assert all(case.expected_route == "tool" for case in failures)


def test_parameter_rejection_fixture_rejects_the_requested_query() -> None:
    executions: list[dict] = []
    tool = build_registry(executions).resolve("test__broken_lookup")
    assert tool is not None
    with pytest.raises(ValueError, match="controlled parameter rejection"):
        asyncio.run(tool.handler({"query": "参数拒绝样例"}, Scope("eval", "eval", "eval", "companion")))
    assert [item["tool"] for item in executions] == ["test__broken_lookup"]
