"""Integration tests for the Orchestrator — using mock agents, no SDK calls."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from agent_harness.core import Orchestrator
from agent_harness.artifacts import (
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
