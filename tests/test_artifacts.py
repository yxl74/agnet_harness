"""Tests for agent_harness/artifacts.py — round-trip serialization, markdown output, and enums."""

import pytest
from agent_harness.artifacts import (
    Contract,
    Task,
    Plan,
    GenerationResult,
    EvaluationResult,
    CheckResult,
    UsageInfo,
    StageExecution,
    Verdict,
)


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_contract() -> Contract:
    return Contract(
        success_criteria=["Returns 200 on GET /health", "DB connection pool initialized"],
        scope_boundaries="Only the HTTP server startup; no authentication changes.",
    )


def _make_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        title="Add health endpoint",
        description="Implement GET /health that returns {status: ok}.",
        acceptance_criteria=["Returns 200", "JSON body includes status key"],
        dependencies=[],
        contract=_make_contract(),
    )


def _make_plan() -> Plan:
    tasks = [
        _make_task("t1"),
        Task(
            id="t2",
            title="Add metrics endpoint",
            description="Expose /metrics for Prometheus scraping.",
            acceptance_criteria=["Returns 200", "Prometheus format"],
            dependencies=["t1"],
            contract=Contract(
                success_criteria=["Prometheus scrape returns counter data"],
                scope_boundaries="Only /metrics; no auth middleware.",
            ),
        ),
    ]
    return Plan(
        task_description="Build a minimal health/metrics API",
        spec="FastAPI app with /health and /metrics endpoints.",
        tasks=tasks,
        acceptance_criteria=["All endpoints reachable", "No 5xx errors"],
        raw_text="# Plan\n\nBuild it.",
    )


def _make_check_result(passed: bool = True) -> CheckResult:
    return CheckResult(
        name="pytest",
        passed=passed,
        output="10 passed" if passed else "1 failed",
        severity="blocking",
    )


def _make_generation_result() -> GenerationResult:
    return GenerationResult(
        task_id="t1",
        summary="Added GET /health to main.py",
        files_changed=["app/main.py", "tests/test_health.py"],
        raw_text="## Generation\n\nDone.",
    )


def _make_evaluation_result(verdict: Verdict = Verdict.ADVANCE_TASK) -> EvaluationResult:
    return EvaluationResult(
        task_id="t1",
        verdict=verdict,
        scores={"correctness": 1.0, "style": 0.9},
        check_results=[_make_check_result(passed=True)],
        feedback="Looks good.",
        replan_reason=None,
        raw_text="## Evaluation\n\nPass.",
    )


def _make_usage_info() -> UsageInfo:
    return UsageInfo(input_tokens=500, output_tokens=250, spend_usd=0.012)


def _make_stage_execution(result=None) -> StageExecution:
    if result is None:
        result = _make_generation_result()
    return StageExecution(
        result=result,
        usage=_make_usage_info(),
        session_key="generator:t1",
        session_id="sess-abc-123",
    )


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_round_trip(self):
        original = _make_contract()
        data = original.to_json()
        reconstructed = Contract.from_json(data)
        assert reconstructed == original

    def test_to_json_keys(self):
        data = _make_contract().to_json()
        assert "success_criteria" in data
        assert "scope_boundaries" in data

    def test_from_json_types(self):
        c = Contract.from_json(
            {"success_criteria": ["a", "b"], "scope_boundaries": "just this"}
        )
        assert isinstance(c.success_criteria, list)
        assert isinstance(c.scope_boundaries, str)


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TestTask:
    def test_round_trip(self):
        original = _make_task()
        data = original.to_json()
        reconstructed = Task.from_json(data)
        assert reconstructed == original

    def test_round_trip_with_dependencies(self):
        task = Task(
            id="t2",
            title="Second",
            description="Depends on t1",
            acceptance_criteria=["works"],
            dependencies=["t1"],
            contract=_make_contract(),
        )
        assert Task.from_json(task.to_json()) == task

    def test_nested_contract_round_trips(self):
        task = _make_task()
        reconstructed = Task.from_json(task.to_json())
        assert reconstructed.contract == task.contract
        assert isinstance(reconstructed.contract, Contract)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class TestPlan:
    def test_round_trip(self):
        original = _make_plan()
        data = original.to_json()
        reconstructed = Plan.from_json(data)
        assert reconstructed == original

    def test_nested_tasks_round_trip(self):
        plan = _make_plan()
        reconstructed = Plan.from_json(plan.to_json())
        assert len(reconstructed.tasks) == 2
        assert reconstructed.tasks[0].id == "t1"
        assert reconstructed.tasks[1].id == "t2"
        assert isinstance(reconstructed.tasks[1].contract, Contract)

    def test_to_markdown_contains_sections(self):
        plan = _make_plan()
        md = plan.to_markdown()
        assert "# Plan" in md
        assert "## Request" in md
        assert "## Spec" in md
        assert "## Tasks" in md
        assert "## Overall Acceptance Criteria" in md
        assert "t1" in md
        assert "t2" in md
        assert "Build a minimal health/metrics API" in md

    def test_to_markdown_includes_contracts(self):
        plan = _make_plan()
        md = plan.to_markdown()
        assert "Contract" in md
        assert "Success Criteria" in md or "success_criteria" in md.lower()


# ---------------------------------------------------------------------------
# GenerationResult
# ---------------------------------------------------------------------------


class TestGenerationResult:
    def test_round_trip(self):
        original = _make_generation_result()
        reconstructed = GenerationResult.from_json(original.to_json())
        assert reconstructed == original

    def test_to_json_keys(self):
        data = _make_generation_result().to_json()
        assert "task_id" in data
        assert "summary" in data
        assert "files_changed" in data
        assert "raw_text" in data

    def test_files_changed_list(self):
        gr = GenerationResult.from_json(_make_generation_result().to_json())
        assert isinstance(gr.files_changed, list)


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_round_trip_passed(self):
        original = _make_check_result(passed=True)
        assert CheckResult.from_json(original.to_json()) == original

    def test_round_trip_failed(self):
        original = _make_check_result(passed=False)
        assert CheckResult.from_json(original.to_json()) == original

    def test_severity_preserved(self):
        cr = CheckResult(name="lint", passed=True, output="ok", severity="warning")
        assert CheckResult.from_json(cr.to_json()).severity == "warning"


# ---------------------------------------------------------------------------
# EvaluationResult
# ---------------------------------------------------------------------------


class TestEvaluationResult:
    def test_round_trip_advance(self):
        original = _make_evaluation_result(Verdict.ADVANCE_TASK)
        reconstructed = EvaluationResult.from_json(original.to_json())
        assert reconstructed == original
        assert reconstructed.verdict == Verdict.ADVANCE_TASK

    def test_round_trip_retry(self):
        original = _make_evaluation_result(Verdict.RETRY_TASK)
        reconstructed = EvaluationResult.from_json(original.to_json())
        assert reconstructed.verdict == Verdict.RETRY_TASK

    def test_round_trip_with_replan_reason(self):
        ev = EvaluationResult(
            task_id="t1",
            verdict=Verdict.REQUEST_REPLAN,
            scores={},
            check_results=[],
            feedback="Scope changed",
            replan_reason="New requirements discovered",
            raw_text="replan",
        )
        reconstructed = EvaluationResult.from_json(ev.to_json())
        assert reconstructed.replan_reason == "New requirements discovered"

    def test_round_trip_nested_check_results(self):
        ev = _make_evaluation_result()
        reconstructed = EvaluationResult.from_json(ev.to_json())
        assert len(reconstructed.check_results) == 1
        assert isinstance(reconstructed.check_results[0], CheckResult)

    def test_to_markdown_contains_verdict(self):
        ev = _make_evaluation_result(Verdict.ADVANCE_TASK)
        md = ev.to_markdown()
        assert "advance_task" in md
        assert "t1" in md

    def test_to_markdown_contains_feedback_section(self):
        ev = _make_evaluation_result()
        md = ev.to_markdown()
        assert "Feedback" in md or "feedback" in md

    def test_to_markdown_halt_run(self):
        ev = EvaluationResult(
            task_id="t2",
            verdict=Verdict.HALT_RUN,
            scores={"quality": 0.2},
            check_results=[_make_check_result(passed=False)],
            feedback="Cannot proceed.",
            replan_reason=None,
            raw_text="halt",
        )
        md = ev.to_markdown()
        assert "halt_run" in md
        assert "t2" in md


# ---------------------------------------------------------------------------
# UsageInfo
# ---------------------------------------------------------------------------


class TestUsageInfo:
    def test_round_trip(self):
        original = _make_usage_info()
        reconstructed = UsageInfo.from_json(original.to_json())
        assert reconstructed == original

    def test_values(self):
        u = UsageInfo.from_json({"input_tokens": 100, "output_tokens": 50, "spend_usd": 0.005})
        assert u.input_tokens == 100
        assert u.output_tokens == 50
        assert u.spend_usd == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# StageExecution
# ---------------------------------------------------------------------------


class TestStageExecution:
    def test_round_trip_with_generation_result(self):
        original = _make_stage_execution(_make_generation_result())
        data = original.to_json()
        reconstructed = StageExecution.from_json(data, GenerationResult)
        assert reconstructed.session_key == original.session_key
        assert reconstructed.session_id == original.session_id
        assert reconstructed.usage == original.usage
        assert reconstructed.result == original.result

    def test_round_trip_with_plan(self):
        plan = _make_plan()
        se = StageExecution(
            result=plan,
            usage=_make_usage_info(),
            session_key="planner:v1",
            session_id="sess-planner-1",
        )
        data = se.to_json()
        reconstructed = StageExecution.from_json(data, Plan)
        assert reconstructed.session_key == "planner:v1"
        assert isinstance(reconstructed.result, Plan)
        assert reconstructed.result.task_description == plan.task_description

    def test_to_json_keys(self):
        data = _make_stage_execution().to_json()
        assert "result" in data
        assert "usage" in data
        assert "session_key" in data
        assert "session_id" in data


# ---------------------------------------------------------------------------
# Nested round-trip: Plan with Tasks and Contracts
# ---------------------------------------------------------------------------


class TestNestedRoundTrip:
    def test_plan_full_tree(self):
        """Plan -> to_json() -> from_json() preserves the full object tree."""
        plan = Plan(
            task_description="Full nested test",
            spec="Multiple tasks with contracts.",
            tasks=[
                Task(
                    id=f"task-{i}",
                    title=f"Task {i}",
                    description=f"Description for task {i}",
                    acceptance_criteria=[f"criterion-{i}-a", f"criterion-{i}-b"],
                    dependencies=[f"task-{i-1}"] if i > 0 else [],
                    contract=Contract(
                        success_criteria=[f"criterion-{i} done"],
                        scope_boundaries=f"Scope for task {i}",
                    ),
                )
                for i in range(3)
            ],
            acceptance_criteria=["all tasks complete", "no regressions"],
            raw_text="raw",
        )

        reconstructed = Plan.from_json(plan.to_json())

        assert reconstructed.task_description == plan.task_description
        assert len(reconstructed.tasks) == 3
        for i, (orig, recon) in enumerate(zip(plan.tasks, reconstructed.tasks)):
            assert orig == recon
            assert isinstance(recon.contract, Contract)
            assert recon.contract.success_criteria == orig.contract.success_criteria


# ---------------------------------------------------------------------------
# Verdict enum
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_advance_task_value(self):
        assert Verdict.ADVANCE_TASK.value == "advance_task"

    def test_retry_task_value(self):
        assert Verdict.RETRY_TASK.value == "retry_task"

    def test_request_replan_value(self):
        assert Verdict.REQUEST_REPLAN.value == "request_replan"

    def test_halt_run_value(self):
        assert Verdict.HALT_RUN.value == "halt_run"

    def test_construct_from_string_advance(self):
        assert Verdict("advance_task") == Verdict.ADVANCE_TASK

    def test_construct_from_string_retry(self):
        assert Verdict("retry_task") == Verdict.RETRY_TASK

    def test_construct_from_string_replan(self):
        assert Verdict("request_replan") == Verdict.REQUEST_REPLAN

    def test_construct_from_string_halt(self):
        assert Verdict("halt_run") == Verdict.HALT_RUN

    def test_all_four_members(self):
        members = {v.value for v in Verdict}
        assert members == {"advance_task", "retry_task", "request_replan", "halt_run"}
