"""Regression tests for planner and evaluator parsers using captured real model output."""

import pytest
from pathlib import Path
from agent_harness.planner import DefaultPlanner
from agent_harness.evaluator import DefaultEvaluator
from agent_harness.artifacts import Verdict

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evaluator() -> DefaultEvaluator:
    return DefaultEvaluator(system_prompt="", tools=[], model="test", cwd=".")


def _make_planner() -> DefaultPlanner:
    return DefaultPlanner(system_prompt="", tools=[], model="test", cwd=".")


# ---------------------------------------------------------------------------
# Evaluator parser regression tests
# ---------------------------------------------------------------------------


class TestEvaluatorParserRegression:
    """Test _parse_evaluation against real model output captured from live runs."""

    def test_advance_task_parsed_correctly(self):
        """EVALUATION RESULT block with VERDICT: prefix is parsed as ADVANCE_TASK."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        assert result.verdict == Verdict.ADVANCE_TASK

    def test_advance_task_has_scores(self):
        """SCORE: lines from real output are parsed into the scores dict."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        assert len(result.scores) > 0
        assert "correctness" in result.scores
        assert result.scores["correctness"] == 1.0

    def test_advance_task_has_checks(self):
        """CHECK: lines from real output are parsed into check_results."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        assert len(result.check_results) > 0
        check_names = {c.name for c in result.check_results}
        assert "pytest" in check_names

    def test_advance_task_checks_are_passing(self):
        """All checks in the advance fixture are expected to have passed=True."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        for check in result.check_results:
            assert check.passed, f"Expected check '{check.name}' to be passing"

    def test_advance_task_task_id_preserved(self):
        """task_id passed to _parse_evaluation is stored on the result."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        assert result.task_id == "task-01"

    def test_advance_markdown_format_not_misread_as_retry(self):
        """Markdown-heading format (## Verdict / ADVANCE_TASK on next line) must NOT
        be parsed as RETRY_TASK.

        This is the format that broke the old parser.  The model emits:

            ## Verdict
            ADVANCE_TASK

        instead of the canonical ``VERDICT: ADVANCE_TASK`` line.  The current
        parser has a fallback that handles this; this test locks it in.
        """
        text = (FIXTURES / "evaluator_output_advance_markdown_format.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-02")
        assert result.verdict == Verdict.ADVANCE_TASK, (
            f"Expected ADVANCE_TASK but got {result.verdict}. "
            "The markdown-heading fallback parser may be broken."
        )

    def test_advance_markdown_format_task_id(self):
        """task_id is correctly set when parsing the markdown-heading format."""
        text = (FIXTURES / "evaluator_output_advance_markdown_format.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-02")
        assert result.task_id == "task-02"

    def test_empty_text_defaults_to_retry(self):
        """Completely empty agent output must not crash; it defaults to RETRY_TASK."""
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation("", "task-99")
        assert result.verdict == Verdict.RETRY_TASK
        assert result.task_id == "task-99"

    def test_verdict_case_insensitive(self):
        """Verdict matching is case-insensitive (ADVANCE_TASK vs advance_task)."""
        text = "VERDICT: advance_task"
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        assert result.verdict == Verdict.ADVANCE_TASK

    def test_scores_are_floats(self):
        """Score values parsed from real output are valid floats in [0, 1]."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        for criterion, score in result.scores.items():
            assert isinstance(score, float), f"Score for {criterion} is not a float"
            assert 0.0 <= score <= 1.0, f"Score {score} for {criterion} out of range"

    def test_check_severity_captured(self):
        """Severity field is preserved from CHECK: lines."""
        text = (FIXTURES / "evaluator_output_advance.txt").read_text()
        evaluator = _make_evaluator()
        result = evaluator._parse_evaluation(text, "task-01")
        severities = {c.severity for c in result.check_results}
        assert "blocking" in severities


# ---------------------------------------------------------------------------
# Planner parser regression tests
# ---------------------------------------------------------------------------


class TestPlannerParserRegression:
    """Test _parse_plan against real model output captured from live runs."""

    def test_plan_parsed_with_tasks(self):
        """Real planner output containing a fenced JSON block produces a non-empty plan."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        assert len(plan.tasks) > 0

    def test_plan_task_ids(self):
        """Task IDs from the JSON block are correctly extracted."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        ids = [t.id for t in plan.tasks]
        assert "task-01" in ids
        assert "task-02" in ids

    def test_plan_task_titles(self):
        """Every task in the parsed plan has a non-empty title."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        for task in plan.tasks:
            assert task.title, f"Task {task.id} has an empty title"

    def test_plan_task_contracts_present(self):
        """Every task has a Contract with at least one success criterion."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        for task in plan.tasks:
            assert task.contract is not None, f"Task {task.id} has no contract"
            assert len(task.contract.success_criteria) > 0, (
                f"Task {task.id} contract has no success criteria"
            )

    def test_plan_task_descriptions(self):
        """Every task has a non-empty description."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        for task in plan.tasks:
            assert task.description, f"Task {task.id} has an empty description"

    def test_plan_task_description_preserved(self):
        """The task_description passed to _parse_plan is stored on the plan."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        desc = "Create a Python hello world project"
        plan = planner._parse_plan(text, desc)
        assert plan.task_description == desc

    def test_plan_spec_extracted(self):
        """The spec field from the JSON block is extracted and non-empty."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        assert plan.spec, "Expected a non-empty spec from the parsed plan"

    def test_plan_acceptance_criteria(self):
        """Top-level acceptance_criteria list is extracted from the JSON block."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        assert len(plan.acceptance_criteria) > 0

    def test_plan_raw_text_preserved(self):
        """raw_text on the plan contains the original input text."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        assert plan.raw_text == text

    def test_plan_task_dependencies_parsed(self):
        """Dependencies list is parsed; task-02 should depend on task-01."""
        text = (FIXTURES / "planner_output_success.txt").read_text()
        planner = _make_planner()
        plan = planner._parse_plan(text, "Create a Python hello world project")
        task_map = {t.id: t for t in plan.tasks}
        assert "task-02" in task_map
        assert "task-01" in task_map["task-02"].dependencies

    def test_fallback_plan_for_empty_text(self):
        """Empty planner output produces a single-task stub rather than crashing."""
        planner = _make_planner()
        plan = planner._parse_plan("", "some task")
        assert len(plan.tasks) == 1
        assert plan.tasks[0].id == "task-01"

    def test_fallback_plan_for_invalid_json(self):
        """Garbage text produces a single-task stub rather than crashing."""
        planner = _make_planner()
        plan = planner._parse_plan("this is not json at all", "some task")
        assert len(plan.tasks) == 1
