"""Integration tests for the Orchestrator — using mock agents, no SDK calls."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from agent_harness.core import Orchestrator
from agent_harness.artifacts import (
    CheckResult,
    Contract,
    EvaluationResult,
    GenerationResult,
    Plan,
    StageExecution,
    Task,
    UsageInfo,
    Verdict,
)
from agent_harness.state import RunState, RunStatus
from agent_harness.config import HarnessConfig


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config():
    return HarnessConfig(
        name="test",
        model="claude-opus-4-6",
        max_budget_usd=10.0,
        generator_tools=[],
        evaluator_tools=[],
        planner_tools=[],
        target_repo=None,
        session_mode="fresh",
        planner_prompt="",
        generator_prompt="",
        evaluator_prompt="",
    )


@pytest.fixture
def simple_plan():
    return Plan(
        task_description="test task",
        spec="test spec",
        tasks=[
            Task(
                id="t1",
                title="Task 1",
                description="Do thing",
                acceptance_criteria=["works"],
                dependencies=[],
                contract=Contract(
                    success_criteria=["it works"],
                    scope_boundaries="just this",
                ),
            )
        ],
        acceptance_criteria=["all good"],
        raw_text="raw plan text",
    )


@pytest.fixture
def two_task_plan():
    return Plan(
        task_description="two task test",
        spec="two task spec",
        tasks=[
            Task(
                id="t1",
                title="Task 1",
                description="First thing",
                acceptance_criteria=["t1 works"],
                dependencies=[],
                contract=Contract(success_criteria=["t1 done"], scope_boundaries="t1 only"),
            ),
            Task(
                id="t2",
                title="Task 2",
                description="Second thing",
                acceptance_criteria=["t2 works"],
                dependencies=["t1"],
                contract=Contract(success_criteria=["t2 done"], scope_boundaries="t2 only"),
            ),
        ],
        acceptance_criteria=["both good"],
        raw_text="raw two-task plan",
    )


def _planner_exec(plan: Plan, session_key: str = "planner:v1") -> StageExecution:
    return StageExecution(
        result=plan,
        usage=UsageInfo(100, 50, 0.01),
        session_key=session_key,
        session_id="sess-1",
    )


def _generator_exec(task_id: str = "t1", session_key: str = "generator:t1") -> StageExecution:
    return StageExecution(
        result=GenerationResult(
            task_id=task_id,
            summary="did it",
            files_changed=["app.py"],
            raw_text="done",
        ),
        usage=UsageInfo(200, 100, 0.02),
        session_key=session_key,
        session_id="sess-2",
    )


def _evaluator_exec(
    task_id: str = "t1",
    verdict: Verdict = Verdict.ADVANCE_TASK,
    session_key: str = "evaluator:t1:iter-0",
) -> StageExecution:
    return StageExecution(
        result=EvaluationResult(
            task_id=task_id,
            verdict=verdict,
            scores={"correctness": 1.0},
            check_results=[],
            feedback="good" if verdict == Verdict.ADVANCE_TASK else "needs work",
            replan_reason="scope changed" if verdict == Verdict.REQUEST_REPLAN else None,
            raw_text="eval",
        ),
        usage=UsageInfo(150, 75, 0.015),
        session_key=session_key,
        session_id="sess-3",
    )


def _make_orchestrator(
    tmp_path,
    config,
    planner,
    generator,
    evaluator,
) -> Orchestrator:
    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    return Orchestrator(
        planner=planner,
        generator=generator,
        evaluator=evaluator,
        config=config,
        run_dir=run_dir,
    )


# ---------------------------------------------------------------------------
# Happy path: plan -> generate -> evaluate(ADVANCE_TASK) -> completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_happy_path(tmp_path, mock_config, simple_plan):
    """Full loop: plan -> generate -> evaluate(ADVANCE_TASK) -> COMPLETED."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec("t1")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec("t1", Verdict.ADVANCE_TASK)

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.COMPLETED
    assert "t1" in state.completed_task_ids
    assert state.cumulative_spend_usd > 0


@pytest.mark.asyncio
async def test_happy_path_spend_accumulated(tmp_path, mock_config, simple_plan):
    """Spend from planner + generator + evaluator should be summed."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)  # 0.01

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()  # 0.02

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()  # 0.015

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert state.cumulative_spend_usd == pytest.approx(0.045)


@pytest.mark.asyncio
async def test_happy_path_session_ids_registered(tmp_path, mock_config, simple_plan):
    """Session keys from all three agents should be stored in state.session_ids."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan, session_key="planner:v1")

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec(session_key="generator:t1")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec(session_key="evaluator:t1:iter-0")

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert "planner:v1" in state.session_ids
    assert "generator:t1" in state.session_ids
    assert "evaluator:t1:iter-0" in state.session_ids


@pytest.mark.asyncio
async def test_happy_path_two_tasks(tmp_path, mock_config, two_task_plan):
    """Both tasks complete -> COMPLETED with both IDs in completed_task_ids."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(two_task_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.side_effect = [
        _generator_exec("t1"),
        _generator_exec("t2"),
    ]

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _evaluator_exec("t1", Verdict.ADVANCE_TASK),
        _evaluator_exec("t2", Verdict.ADVANCE_TASK),
    ]

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("two task test")

    assert state.status == RunStatus.COMPLETED
    assert "t1" in state.completed_task_ids
    assert "t2" in state.completed_task_ids


# ---------------------------------------------------------------------------
# RETRY_TASK flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_task_then_advance(tmp_path, mock_config, simple_plan):
    """Evaluator returns RETRY_TASK first, then ADVANCE_TASK on second call."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _evaluator_exec("t1", Verdict.RETRY_TASK, "evaluator:t1:iter-0"),
        _evaluator_exec("t1", Verdict.ADVANCE_TASK, "evaluator:t1:iter-1"),
    ]

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.COMPLETED
    assert "t1" in state.completed_task_ids
    # Generator should be called twice (once per iteration)
    assert mock_generator.generate.call_count == 2
    # Evaluator should be called twice
    assert mock_evaluator.evaluate.call_count == 2


@pytest.mark.asyncio
async def test_retry_task_passes_prior_evaluation(tmp_path, mock_config, simple_plan):
    """On retry, the prior evaluation is passed to the generator."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _evaluator_exec("t1", Verdict.RETRY_TASK),
        _evaluator_exec("t1", Verdict.ADVANCE_TASK),
    ]

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    await orchestrator.run("test task")

    # First generate call: prior_evaluation=None
    first_call_args = mock_generator.generate.call_args_list[0]
    assert first_call_args.args[1] is None or first_call_args.kwargs.get("prior_evaluation") is None

    # Second generate call: prior_evaluation should be the first eval result
    second_call_args = mock_generator.generate.call_args_list[1]
    prior_eval = second_call_args.args[1] if len(second_call_args.args) > 1 else second_call_args.kwargs.get("prior_evaluation")
    assert prior_eval is not None
    assert isinstance(prior_eval, EvaluationResult)
    assert prior_eval.verdict == Verdict.RETRY_TASK


# ---------------------------------------------------------------------------
# HALT_RUN flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halt_run_sets_halted_status(tmp_path, mock_config, simple_plan):
    """When evaluator returns HALT_RUN, run status becomes HALTED."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec("t1", Verdict.HALT_RUN)

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.HALTED
    assert "t1" not in state.completed_task_ids


@pytest.mark.asyncio
async def test_halt_run_stops_loop(tmp_path, mock_config, two_task_plan):
    """After HALT_RUN on task 1, task 2 should never be generated."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(two_task_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec("t1")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec("t1", Verdict.HALT_RUN)

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("two task test")

    assert state.status == RunStatus.HALTED
    assert mock_generator.generate.call_count == 1


@pytest.mark.asyncio
async def test_halt_run_sets_reason(tmp_path, mock_config, simple_plan):
    """HALT_RUN feedback should be stored as status_reason."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    halt_eval = StageExecution(
        result=EvaluationResult(
            task_id="t1",
            verdict=Verdict.HALT_RUN,
            scores={},
            check_results=[],
            feedback="Quality gate permanently failed.",
            replan_reason=None,
            raw_text="halt",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-0",
        session_id="sess-halt",
    )

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = halt_eval

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.HALTED
    assert state.status_reason == "Quality gate permanently failed."


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhaustion_pauses_run(tmp_path, simple_plan):
    """When max_budget_usd is very low, run should be PAUSED."""
    tight_config = HarnessConfig(
        name="tight",
        model="claude-opus-4-6",
        max_budget_usd=0.005,  # Less than one planner call (0.01)
        generator_tools=[],
        evaluator_tools=[],
        planner_tools=[],
        target_repo=None,
        session_mode="fresh",
        planner_prompt="",
        generator_prompt="",
        evaluator_prompt="",
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)  # costs 0.01

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    orchestrator = _make_orchestrator(
        tmp_path, tight_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.PAUSED
    assert state.status_reason == "budget_exhausted"


@pytest.mark.asyncio
async def test_budget_exhaustion_after_first_task(tmp_path, two_task_plan):
    """Budget runs out at the start of the second task's loop iteration.

    Spend trace: planner(0.01) + gen1(0.02) + eval1(0.015) = 0.045.
    The budget check fires at the top of the inner loop before generate, so
    setting max_budget_usd=0.045 means 0.045 >= 0.045 is True -> PAUSED.
    """
    mid_config = HarnessConfig(
        name="mid",
        model="claude-opus-4-6",
        max_budget_usd=0.045,  # Exactly equal to spend after t1 completes
        generator_tools=[],
        evaluator_tools=[],
        planner_tools=[],
        target_repo=None,
        session_mode="fresh",
        planner_prompt="",
        generator_prompt="",
        evaluator_prompt="",
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(two_task_plan)  # 0.01

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec("t1")  # 0.02

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec("t1", Verdict.ADVANCE_TASK)  # 0.015

    orchestrator = _make_orchestrator(
        tmp_path, mid_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("two task test")

    # t1 completed, t2 stalls due to budget
    assert "t1" in state.completed_task_ids
    assert state.status == RunStatus.PAUSED


# ---------------------------------------------------------------------------
# REQUEST_REPLAN flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_replan_calls_planner_again(tmp_path, mock_config, simple_plan):
    """Evaluator returns REQUEST_REPLAN -> planner is called again."""
    revised_plan = Plan(
        task_description="test task",
        spec="revised spec",
        tasks=[
            Task(
                id="t1-revised",
                title="Revised Task 1",
                description="Do revised thing",
                acceptance_criteria=["revised works"],
                dependencies=[],
                contract=Contract(
                    success_criteria=["revised done"],
                    scope_boundaries="revised scope",
                ),
            )
        ],
        acceptance_criteria=["revised all good"],
        raw_text="revised raw plan",
    )

    mock_planner = AsyncMock()
    mock_planner.plan.side_effect = [
        _planner_exec(simple_plan, "planner:v1"),
        _planner_exec(revised_plan, "planner:v2"),
    ]

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec("t1-revised")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _evaluator_exec("t1", Verdict.REQUEST_REPLAN),
        _evaluator_exec("t1-revised", Verdict.ADVANCE_TASK),
    ]

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("test task")

    assert mock_planner.plan.call_count == 2
    assert state.plan_version == 1


@pytest.mark.asyncio
async def test_request_replan_passes_context_to_planner(tmp_path, mock_config, simple_plan):
    """The replan_reason from EvaluationResult is forwarded to the second planner call."""
    revised_plan = Plan(
        task_description="test task",
        spec="revised",
        tasks=[
            Task(
                id="t1-r",
                title="Revised",
                description="Rev",
                acceptance_criteria=["ok"],
                dependencies=[],
                contract=Contract(success_criteria=["done"], scope_boundaries="scope"),
            )
        ],
        acceptance_criteria=["ok"],
        raw_text="revised",
    )

    replan_eval = StageExecution(
        result=EvaluationResult(
            task_id="t1",
            verdict=Verdict.REQUEST_REPLAN,
            scores={},
            check_results=[],
            feedback="needs replan",
            replan_reason="scope changed significantly",
            raw_text="replan eval",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-0",
        session_id="sess-eval",
    )

    mock_planner = AsyncMock()
    mock_planner.plan.side_effect = [
        _planner_exec(simple_plan),
        _planner_exec(revised_plan, "planner:v2"),
    ]

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec("t1-r")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        replan_eval,
        _evaluator_exec("t1-r", Verdict.ADVANCE_TASK),
    ]

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    await orchestrator.run("test task")

    # Check that the second planner call received the replan context
    second_call = mock_planner.plan.call_args_list[1]
    # Could be positional or keyword
    all_args = list(second_call.args) + list(second_call.kwargs.values())
    assert "scope changed significantly" in all_args or \
        second_call.kwargs.get("replan_context") == "scope changed significantly"


@pytest.mark.asyncio
async def test_request_replan_completed_task_ids_forwarded(tmp_path, mock_config, two_task_plan):
    """After completing t1, replan should forward completed_task_ids to planner."""
    revised_plan = Plan(
        task_description="two task test",
        spec="revised",
        tasks=[
            Task(
                id="t1",
                title="Task 1 (already done)",
                description="Already done",
                acceptance_criteria=["done"],
                dependencies=[],
                contract=Contract(success_criteria=["done"], scope_boundaries="t1"),
            ),
            Task(
                id="t2-r",
                title="Revised Task 2",
                description="New t2",
                acceptance_criteria=["new t2 ok"],
                dependencies=["t1"],
                contract=Contract(success_criteria=["new t2 done"], scope_boundaries="t2-r"),
            ),
        ],
        acceptance_criteria=["both ok"],
        raw_text="revised two task plan",
    )

    mock_planner = AsyncMock()
    mock_planner.plan.side_effect = [
        _planner_exec(two_task_plan),
        _planner_exec(revised_plan, "planner:v2"),
    ]

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec("t1")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _evaluator_exec("t1", Verdict.ADVANCE_TASK),  # t1 passes
        _evaluator_exec("t2", Verdict.REQUEST_REPLAN),  # t2 triggers replan
        _evaluator_exec("t2-r", Verdict.ADVANCE_TASK),  # revised t2 passes
    ]

    mock_generator.generate.side_effect = [
        _generator_exec("t1"),
        _generator_exec("t2"),
        _generator_exec("t2-r"),
    ]

    orchestrator = _make_orchestrator(
        tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator
    )
    state = await orchestrator.run("two task test")

    assert mock_planner.plan.call_count == 2
    # completed_task_ids should have been forwarded in second call
    second_planner_call = mock_planner.plan.call_args_list[1]
    forwarded_ids = second_planner_call.kwargs.get("completed_task_ids", [])
    assert "t1" in forwarded_ids


# ---------------------------------------------------------------------------
# Artifacts written to disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_written_to_run_dir(tmp_path, mock_config, simple_plan):
    """plan.json and plan.md should be written after planning."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    run_dir = tmp_path / "runs" / "artifact-test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(
        planner=mock_planner,
        generator=mock_generator,
        evaluator=mock_evaluator,
        config=mock_config,
        run_dir=run_dir,
    )
    await orchestrator.run("test task")

    assert (run_dir / "plan.json").exists()
    assert (run_dir / "plan.md").exists()
    assert (run_dir / "run_state.json").exists()


@pytest.mark.asyncio
async def test_task_artifacts_written(tmp_path, mock_config, simple_plan):
    """Generation and evaluation artifacts should be written under tasks/."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    run_dir = tmp_path / "runs" / "task-artifacts-test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(
        planner=mock_planner,
        generator=mock_generator,
        evaluator=mock_evaluator,
        config=mock_config,
        run_dir=run_dir,
    )
    await orchestrator.run("test task")

    tasks_dir = run_dir / "tasks"
    assert tasks_dir.exists()
    # At least one task subdir should exist
    task_subdirs = list(tasks_dir.iterdir())
    assert len(task_subdirs) >= 1


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_state_persisted_at_completion(tmp_path, mock_config, simple_plan):
    """run_state.json should be written with COMPLETED status when run finishes."""
    import json as json_module

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    run_dir = tmp_path / "runs" / "state-test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(
        planner=mock_planner,
        generator=mock_generator,
        evaluator=mock_evaluator,
        config=mock_config,
        run_dir=run_dir,
    )
    await orchestrator.run("test task")

    state_path = run_dir / "run_state.json"
    assert state_path.exists()
    data = json_module.loads(state_path.read_text())
    assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# Resume tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_paused_run_does_not_replan(tmp_path, mock_config, simple_plan):
    """Resume a paused run: planner should NOT be called again."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # First call: budget_exhausted (pause). Second call: ADVANCE_TASK
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    # --- First run: exhaust budget to trigger pause ---
    # Plan costs 0.01, gen costs 0.02, eval costs 0.015 → total 0.045
    # Budget check happens before generate; after plan (0.01) it passes,
    # after plan+gen+eval (0.045) the next budget check triggers pause.
    # So we need the task to retry once (two gen/eval cycles):
    # first eval returns RETRY, second budget check catches it.
    retry_eval = _evaluator_exec(verdict=Verdict.RETRY_TASK)
    mock_evaluator.evaluate.side_effect = [retry_eval, _evaluator_exec()]

    low_budget_config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=0.045,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
    )
    run_dir = tmp_path / "runs" / "resume-test"
    run_dir.mkdir(parents=True)
    orch = Orchestrator(
        planner=mock_planner, generator=mock_generator, evaluator=mock_evaluator,
        config=low_budget_config, run_dir=run_dir,
    )
    state = await orch.run("test task")
    assert state.status == RunStatus.PAUSED
    planner_calls_before = mock_planner.plan.call_count

    # --- Resume: increase budget, planner should NOT be called again ---
    resumed_config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=100.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
    )
    orch2 = Orchestrator(
        planner=mock_planner, generator=mock_generator, evaluator=mock_evaluator,
        config=resumed_config, run_dir=run_dir,
    )
    orch2.state = RunState.load(run_dir / "run_state.json")
    state2 = await orch2.resume_run("test task")

    assert state2.status == RunStatus.COMPLETED
    assert mock_planner.plan.call_count == planner_calls_before  # No new planner calls


@pytest.mark.asyncio
async def test_resume_failed_run_without_plan_replans(tmp_path, mock_config, simple_plan):
    """Resume a FAILED run that never completed planning: should re-plan."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    run_dir = tmp_path / "runs" / "failed-early"
    run_dir.mkdir(parents=True)

    # Simulate a run that failed before plan.json was written
    state = RunState.new("failed-early")
    state.transition(RunStatus.FAILED, reason="agent crash during planning")
    state.save(run_dir / "run_state.json")

    orch = Orchestrator(
        planner=mock_planner, generator=mock_generator, evaluator=mock_evaluator,
        config=mock_config, run_dir=run_dir,
    )
    orch.state = state
    result = await orch.resume_run("test task")

    assert result.status == RunStatus.COMPLETED
    assert mock_planner.plan.call_count == 1  # Had to re-plan


@pytest.mark.asyncio
async def test_resume_after_retry_restores_evaluation_context(tmp_path, mock_config, simple_plan):
    """Resume after RETRY_TASK: generator should receive the persisted evaluation."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # First call: RETRY, then budget pause. After resume: ADVANCE.
    retry_eval = _evaluator_exec(verdict=Verdict.RETRY_TASK, session_key="evaluator:t1:iter-0")
    advance_eval = _evaluator_exec(verdict=Verdict.ADVANCE_TASK, session_key="evaluator:t1:iter-1")

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [retry_eval, advance_eval]

    # Budget just enough for plan + one generate/evaluate cycle, then pause
    tight_config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=0.045,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
    )
    run_dir = tmp_path / "runs" / "retry-resume"
    run_dir.mkdir(parents=True)
    orch = Orchestrator(
        planner=mock_planner, generator=mock_generator, evaluator=mock_evaluator,
        config=tight_config, run_dir=run_dir,
    )
    state = await orch.run("test task")
    assert state.status == RunStatus.PAUSED
    assert state.iterations_on_current_task >= 1

    # Resume with higher budget
    high_budget_config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=100.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
    )
    orch2 = Orchestrator(
        planner=mock_planner, generator=mock_generator, evaluator=mock_evaluator,
        config=high_budget_config, run_dir=run_dir,
    )
    orch2.state = RunState.load(run_dir / "run_state.json")
    state2 = await orch2.resume_run("test task")

    assert state2.status == RunStatus.COMPLETED

    # The generator's second call (after resume) should have received
    # the persisted evaluation, not None
    resumed_gen_call = mock_generator.generate.call_args_list[-1]
    prior_eval_arg = resumed_gen_call[0][1]  # second positional arg
    assert prior_eval_arg is not None
    assert prior_eval_arg.verdict == Verdict.RETRY_TASK


# ---------------------------------------------------------------------------
# No-progress and retry-budget guardrails (P0.3)
# ---------------------------------------------------------------------------


def _retry_eval_exec(
    task_id: str = "t1",
    feedback: str = "needs work",
    check_results: list | None = None,
    session_key: str = "evaluator:t1:iter-0",
) -> StageExecution:
    """Build a RETRY_TASK StageExecution with custom feedback and check_results."""
    return StageExecution(
        result=EvaluationResult(
            task_id=task_id,
            verdict=Verdict.RETRY_TASK,
            scores={},
            check_results=check_results or [],
            feedback=feedback,
            replan_reason=None,
            raw_text="retry eval",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key=session_key,
        session_id="sess-retry",
    )


def _make_retries_config(
    max_retries_per_task: int = 5,
    no_progress_threshold: int = 3,
) -> HarnessConfig:
    return HarnessConfig(
        name="test-retries",
        model="claude-opus-4-6",
        max_budget_usd=100.0,
        generator_tools=[],
        evaluator_tools=[],
        planner_tools=[],
        target_repo=None,
        session_mode="fresh",
        planner_prompt="",
        generator_prompt="",
        evaluator_prompt="",
        max_retries_per_task=max_retries_per_task,
        no_progress_threshold=no_progress_threshold,
    )


@pytest.mark.asyncio
async def test_max_retries_pauses_run(tmp_path, simple_plan):
    """Evaluator always returns RETRY_TASK: run pauses after max_retries_per_task iterations."""
    config = _make_retries_config(max_retries_per_task=3, no_progress_threshold=99)

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    # Always returns RETRY with different feedback so no-progress won't trigger first
    mock_evaluator.evaluate.side_effect = [
        _retry_eval_exec(feedback=f"attempt {i}") for i in range(10)
    ]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.PAUSED
    assert state.status_reason == "max_retries_exceeded"
    # Should have retried exactly max_retries_per_task times (3 evaluate calls)
    assert mock_evaluator.evaluate.call_count == 3


@pytest.mark.asyncio
async def test_no_progress_detection(tmp_path, simple_plan):
    """Repeated identical failures pause the run after no_progress_threshold repetitions."""
    config = _make_retries_config(max_retries_per_task=99, no_progress_threshold=2)

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    identical_feedback = "test always fails the same way"
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _retry_eval_exec(feedback=identical_feedback) for _ in range(10)
    ]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.PAUSED
    assert "no_progress_detected" in state.status_reason
    # Should pause after 2 identical fingerprints
    assert mock_evaluator.evaluate.call_count == 2


@pytest.mark.asyncio
async def test_different_feedback_does_not_trigger_no_progress(tmp_path, simple_plan):
    """Varying feedback avoids no-progress detection; run hits max_retries instead."""
    config = _make_retries_config(max_retries_per_task=4, no_progress_threshold=3)

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Each retry has unique feedback -> different fingerprints
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _retry_eval_exec(feedback=f"unique error #{i}") for i in range(10)
    ]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.PAUSED
    assert state.status_reason == "max_retries_exceeded"
    assert mock_evaluator.evaluate.call_count == 4


@pytest.mark.asyncio
async def test_fingerprint_resets_on_new_task(tmp_path, two_task_plan):
    """Fingerprint history from task 1 does not bleed into task 2."""
    config = _make_retries_config(max_retries_per_task=99, no_progress_threshold=2)

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(two_task_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Task 1: one retry with the same feedback, then advance
    # Task 2: one retry with that same feedback, then advance
    # If fingerprints weren't reset, task 2's first retry would already be a repeat of
    # task 1's retry and could trigger no_progress_threshold=2 on the second retry.
    # With proper reset, task 2 should need 2 retries before pausing.
    same_feedback = "persistent error"
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _retry_eval_exec(task_id="t1", feedback=same_feedback),   # t1: retry 1
        _evaluator_exec("t1", Verdict.ADVANCE_TASK),               # t1: advance
        _retry_eval_exec(task_id="t2", feedback=same_feedback),   # t2: retry 1
        _evaluator_exec("t2", Verdict.ADVANCE_TASK),               # t2: advance
    ]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("two task test")

    # Both tasks should complete — fingerprint reset on task advance means
    # task 2's single retry doesn't combine with task 1's retry to hit threshold=2
    assert state.status == RunStatus.COMPLETED
    assert "t1" in state.completed_task_ids
    assert "t2" in state.completed_task_ids


@pytest.mark.asyncio
async def test_alternating_feedback_does_not_trigger_no_progress(tmp_path, simple_plan):
    """A, B, A pattern should NOT trigger no-progress (consecutive streak, not total count)."""
    config = _make_retries_config(max_retries_per_task=6, no_progress_threshold=2)

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Alternating: A, B, A, B, A, B — never 2 consecutive identical, should hit max_retries
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [
        _retry_eval_exec(feedback="error A"),
        _retry_eval_exec(feedback="error B"),
        _retry_eval_exec(feedback="error A"),
        _retry_eval_exec(feedback="error B"),
        _retry_eval_exec(feedback="error A"),
        _retry_eval_exec(feedback="error B"),
        _retry_eval_exec(feedback="error A"),
    ]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.PAUSED
    assert state.status_reason == "max_retries_exceeded"


# ---------------------------------------------------------------------------
# Stage failure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_exception_results_in_failed_state(tmp_path, mock_config):
    """A stage exception (e.g., ValueError from structured output) should
    be caught and persisted as RunStatus.FAILED, not crash the process."""
    mock_planner = AsyncMock()
    mock_planner.plan.side_effect = ValueError("Planner returned invalid JSON")

    mock_generator = AsyncMock()
    mock_evaluator = AsyncMock()

    orchestrator = _make_orchestrator(tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.FAILED
    assert "ValueError" in state.status_reason
    assert "invalid JSON" in state.status_reason
    # State should be persisted to disk
    state_path = orchestrator.run_dir / "run_state.json"
    assert state_path.exists()


@pytest.mark.asyncio
async def test_evaluator_exception_results_in_failed_state(tmp_path, mock_config, simple_plan):
    """Evaluator raising ValueError (bad structured output) should FAIL the run."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = ValueError("Evaluator returned invalid JSON")

    orchestrator = _make_orchestrator(tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.FAILED
    assert "ValueError" in state.status_reason


# ---------------------------------------------------------------------------
# Threshold enforcement tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_overrides_advance_to_retry(tmp_path, simple_plan):
    """Evaluator says ADVANCE but score below threshold → harness forces RETRY."""
    config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=10.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
        score_thresholds={"correctness": 0.9},  # gate at 0.9
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Evaluator says ADVANCE but correctness is only 0.7 (below 0.9 threshold)
    low_score_advance = StageExecution(
        result=EvaluationResult(
            task_id="t1", verdict=Verdict.ADVANCE_TASK,
            scores={"correctness": 0.7, "quality": 1.0},
            check_results=[], feedback="looks good", replan_reason=None, raw_text="",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-0", session_id="s1",
    )
    # Second eval: score now above threshold → actually advances
    passing_advance = _evaluator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [low_score_advance, passing_advance]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.COMPLETED
    # Generator should have been called twice (first attempt overridden to retry)
    assert mock_generator.generate.call_count == 2


@pytest.mark.asyncio
async def test_threshold_does_not_override_halt(tmp_path, simple_plan):
    """Threshold enforcement only overrides ADVANCE, never HALT."""
    config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=10.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
        score_thresholds={"correctness": 0.9},
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)

    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec(verdict=Verdict.HALT_RUN)

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.HALTED


# ---------------------------------------------------------------------------
# Evaluation dimension enforcement tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_dimension_pass_fail_check_blocks_advance(tmp_path, simple_plan):
    """A declared pass_fail check that fails should override ADVANCE to RETRY."""
    config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=10.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
        evaluation_dimensions=[{
            "name": "data_quality",
            "description": "Data checks",
            "severity": "blocking",
            "checks": [
                {"name": "no_leakage", "check_type": "pass_fail", "description": "No data leakage"},
            ],
        }],
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)
    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Evaluator says ADVANCE but the pass_fail check is not in check_results (missing = failed)
    no_check_advance = StageExecution(
        result=EvaluationResult(
            task_id="t1", verdict=Verdict.ADVANCE_TASK,
            scores={}, check_results=[],
            feedback="looks good", replan_reason=None, raw_text="",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-0", session_id="s1",
    )
    # Second eval: check reported and passes
    with_check_advance = StageExecution(
        result=EvaluationResult(
            task_id="t1", verdict=Verdict.ADVANCE_TASK,
            scores={},
            check_results=[CheckResult(name="no_leakage", passed=True, output="clean", severity="blocking")],
            feedback="all good", replan_reason=None, raw_text="",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-1", session_id="s2",
    )

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [no_check_advance, with_check_advance]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.COMPLETED
    assert mock_generator.generate.call_count == 2  # First attempt was overridden to retry


@pytest.mark.asyncio
async def test_declared_dimension_metric_check_blocks_advance(tmp_path, simple_plan):
    """A declared metric check below threshold should override ADVANCE to RETRY."""
    config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=10.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
        evaluation_dimensions=[{
            "name": "model_perf",
            "description": "Model performance",
            "severity": "blocking",
            "checks": [
                {"name": "pr_auc", "check_type": "metric", "threshold": 0.8, "description": "PR-AUC >= 0.8"},
            ],
        }],
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)
    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Evaluator says ADVANCE but pr_auc is only 0.65 (below 0.8)
    low_metric = StageExecution(
        result=EvaluationResult(
            task_id="t1", verdict=Verdict.ADVANCE_TASK,
            scores={"pr_auc": 0.65}, check_results=[],
            feedback="model trained", replan_reason=None, raw_text="",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-0", session_id="s1",
    )
    # Second eval: metric now above threshold
    good_metric = StageExecution(
        result=EvaluationResult(
            task_id="t1", verdict=Verdict.ADVANCE_TASK,
            scores={"pr_auc": 0.9}, check_results=[],
            feedback="improved", replan_reason=None, raw_text="",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-1", session_id="s2",
    )

    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.side_effect = [low_metric, good_metric]

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.COMPLETED
    assert mock_generator.generate.call_count == 2


@pytest.mark.asyncio
async def test_warning_dimension_does_not_block(tmp_path, simple_plan):
    """A warning-severity dimension should not override ADVANCE even if check fails."""
    config = HarnessConfig(
        name="test", model="claude-opus-4-6", max_budget_usd=10.0,
        generator_tools=[], evaluator_tools=[], planner_tools=[],
        target_repo=None, session_mode="fresh",
        planner_prompt="", generator_prompt="", evaluator_prompt="",
        evaluation_dimensions=[{
            "name": "operational",
            "description": "Ops checks",
            "severity": "warning",
            "checks": [
                {"name": "artifacts_versioned", "check_type": "pass_fail", "description": "Artifacts versioned"},
            ],
        }],
    )

    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)
    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()

    # Warning check missing — should NOT block
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = StageExecution(
        result=EvaluationResult(
            task_id="t1", verdict=Verdict.ADVANCE_TASK,
            scores={}, check_results=[],
            feedback="done", replan_reason=None, raw_text="",
        ),
        usage=UsageInfo(100, 50, 0.01),
        session_key="evaluator:t1:iter-0", session_id="s1",
    )

    orchestrator = _make_orchestrator(tmp_path, config, mock_planner, mock_generator, mock_evaluator)
    state = await orchestrator.run("test task")

    assert state.status == RunStatus.COMPLETED
    assert mock_generator.generate.call_count == 1  # No retry — warning doesn't block


@pytest.mark.asyncio
async def test_evaluation_progress_persisted(tmp_path, mock_config, simple_plan):
    """evaluation_progress.json should be written after each evaluation."""
    mock_planner = AsyncMock()
    mock_planner.plan.return_value = _planner_exec(simple_plan)
    mock_generator = AsyncMock()
    mock_generator.generate.return_value = _generator_exec()
    mock_evaluator = AsyncMock()
    mock_evaluator.evaluate.return_value = _evaluator_exec()

    orchestrator = _make_orchestrator(tmp_path, mock_config, mock_planner, mock_generator, mock_evaluator)
    await orchestrator.run("test task")

    progress_path = orchestrator.run_dir / "evaluation_progress.json"
    assert progress_path.exists()

    import json as _json
    data = _json.loads(progress_path.read_text())
    assert len(data["entries"]) >= 1
    assert data["entries"][0]["task_id"] == "t1"
    assert data["entries"][0]["verdict"] == "advance_task"
