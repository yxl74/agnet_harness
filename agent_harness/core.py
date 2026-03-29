"""Core orchestrator and protocol definitions for the agent harness."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent_harness.artifacts import (
    EvaluationDimension,
    EvaluationProgress,
    EvaluationProgressEntry,
    EvaluationResult,
    GenerationResult,
    Plan,
    StageExecution,
    Task,
    Verdict,
)
from agent_harness.config import HarnessConfig
from agent_harness.state import RunState, RunStatus


# ---------------------------------------------------------------------------
# Protocol interfaces — Step 4's SDK implementations satisfy these
# ---------------------------------------------------------------------------


class Planner(Protocol):
    async def plan(
        self,
        task: str,
        cwd: str,
        replan_context: str | None = None,
        completed_task_ids: list[str] | None = None,
    ) -> StageExecution[Plan]: ...


class Generator(Protocol):
    async def generate(
        self,
        task: Task,
        prior_evaluation: EvaluationResult | None,
        cwd: str,
    ) -> StageExecution[GenerationResult]: ...


class Evaluator(Protocol):
    async def evaluate(
        self,
        task: Task,
        result: GenerationResult,
        cwd: str,
    ) -> StageExecution[EvaluationResult]: ...


# ---------------------------------------------------------------------------
# Orchestrator — drives the plan -> generate -> evaluate loop
# ---------------------------------------------------------------------------


class Orchestrator:
    """Drives the harness loop: plan, generate, evaluate, repeat.

    The orchestrator owns:
    - Run state persistence (saved after every mutation)
    - Git commits (after evaluation passes, not during generation)
    - Artifact writing (plan, generation, evaluation — JSON + Markdown)
    - Budget enforcement and iteration control

    Agent implementations (Planner, Generator, Evaluator) are injected at
    construction time or via the ``from_project`` / ``resume`` factories.
    """

    def __init__(
        self,
        planner: Planner,
        generator: Generator,
        evaluator: Evaluator,
        config: HarnessConfig,
        run_dir: Path,
    ) -> None:
        self.planner = planner
        self.generator = generator
        self.evaluator = evaluator
        self.config = config
        self.run_dir = Path(run_dir)
        self.state = RunState.new(run_id=self.run_dir.name)
        self._failure_fingerprints: list[str] = []
        self._eval_progress = EvaluationProgress.load(self.run_dir / "evaluation_progress.json")

        # Parse declared evaluation dimensions
        if config.evaluation_dimensions:
            self._dimensions = [EvaluationDimension.from_json(d) for d in config.evaluation_dimensions]
        else:
            self._dimensions = []
        if config.target_repo:
            self.project_cwd = str(Path(config.target_repo).resolve())
        else:
            project_path = self.run_dir / "project"
            project_path.mkdir(parents=True, exist_ok=True)
            self.project_cwd = str(project_path)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, task_description: str) -> RunState:
        """Start a new harness run: plan from scratch, then execute tasks."""
        try:
            plan = await self._do_plan(task_description)
            return await self._execute_tasks(task_description, plan)
        except Exception as exc:
            self.state.transition(
                RunStatus.FAILED,
                reason=f"Stage error: {type(exc).__name__}: {exc}",
            )
            self._save_state()
            return self.state

    async def resume_run(self, task_description: str) -> RunState:
        """Resume an existing run from persisted state.

        If planning completed (plan.json exists), loads the plan and
        continues from the current task index without re-invoking the
        planner. If planning never completed (e.g., FAILED during
        planning), restarts from the planning phase.

        Note: The generator's SDK conversation session is NOT restored
        across process restarts — it starts a fresh session. However, the
        prior evaluation feedback IS restored from persisted artifacts,
        which provides the meaningful context for retry behavior.
        """
        try:
            plan_path = self.run_dir / "plan.json"

            if not plan_path.exists():
                plan = await self._do_plan(task_description)
            else:
                plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
                plan = Plan.from_json(plan_data)
                self.state.transition(RunStatus.EXECUTING)
                self._save_state()

            prior_evaluation = self._load_prior_evaluation()
            return await self._execute_tasks(task_description, plan, prior_evaluation)
        except Exception as exc:
            self.state.transition(
                RunStatus.FAILED,
                reason=f"Stage error: {type(exc).__name__}: {exc}",
            )
            self._save_state()
            return self.state

    async def _do_plan(
        self,
        task_description: str,
        replan_context: str | None = None,
    ) -> Plan:
        """Invoke the planner and adopt the resulting plan."""
        plan_exec = await self.planner.plan(
            task_description,
            cwd=self.project_cwd,
            replan_context=replan_context,
            completed_task_ids=self.state.completed_task_ids if replan_context else None,
        )
        plan = plan_exec.result
        self._track(plan_exec)
        if replan_context:
            self.state.plan_version += 1
        self._adopt_plan(plan)
        self._write_artifacts(plan)
        self.state.transition(RunStatus.EXECUTING)
        self._save_state()
        return plan

    async def _execute_tasks(
        self,
        task_description: str,
        plan: Plan,
        initial_prior_evaluation: EvaluationResult | None = None,
    ) -> RunState:
        """Execute the per-task generate/evaluate loop."""
        first_task = True
        while self.state.current_task_index < len(plan.tasks):
            task = plan.tasks[self.state.current_task_index]

            # Skip already-completed tasks (from a prior plan version)
            if task.id in self.state.completed_task_ids:
                self.state.advance_task()
                self._save_state()
                continue

            # On resume, seed the first task with persisted evaluation context
            if first_task and initial_prior_evaluation is not None:
                prior_evaluation: EvaluationResult | None = initial_prior_evaluation
            else:
                prior_evaluation = None
            first_task = False

            # Reset failure fingerprint history for this task
            self._failure_fingerprints = []

            while True:
                # Budget check
                if self.state.cumulative_spend_usd >= self.config.max_budget_usd:
                    self.state.transition(
                        RunStatus.PAUSED, reason="budget_exhausted"
                    )
                    self._save_state()
                    return self.state

                # Generate
                gen_exec = await self.generator.generate(
                    task, prior_evaluation, cwd=self.project_cwd
                )
                self._track(gen_exec)
                self._write_task_artifacts("generation", task, gen_exec.result)
                self._save_state()

                # Evaluate
                eval_exec = await self.evaluator.evaluate(
                    task, gen_exec.result, cwd=self.project_cwd
                )
                self._track(eval_exec)
                evaluation = eval_exec.result

                # --- Threshold enforcement ---
                # The orchestrator enforces hard score thresholds, overriding
                # the evaluator's verdict if any score falls below its gate.
                # This prevents evaluator leniency — the exact problem the
                # Anthropic blog identified.
                was_overridden = evaluation.verdict == Verdict.ADVANCE_TASK
                evaluation = self._enforce_thresholds(evaluation)
                was_overridden = was_overridden and evaluation.verdict != Verdict.ADVANCE_TASK

                # --- Track evaluation progress ---
                self._record_eval_progress(evaluation, task, was_overridden)

                self._write_task_artifacts("evaluation", task, evaluation)
                self.state.record_attempt()
                self._save_state()

                match evaluation.verdict:
                    case Verdict.ADVANCE_TASK:
                        self._commit_checkpoint(task)
                        self.state.completed_task_ids.append(task.id)
                        self.state.advance_task()
                        self._failure_fingerprints = []
                        self._save_state()
                        break

                    case Verdict.RETRY_TASK:
                        prior_evaluation = evaluation

                        # --- Retry cap ---
                        if self.state.iterations_on_current_task >= self.config.max_retries_per_task:
                            self.state.transition(
                                RunStatus.PAUSED, reason="max_retries_exceeded"
                            )
                            self._save_state()
                            return self.state

                        # --- No-progress detection (consecutive streak) ---
                        fingerprint = self._compute_failure_fingerprint(evaluation)
                        self._failure_fingerprints.append(fingerprint)
                        # Count consecutive identical fingerprints at the tail
                        streak = 0
                        for fp in reversed(self._failure_fingerprints):
                            if fp == fingerprint:
                                streak += 1
                            else:
                                break
                        if streak >= self.config.no_progress_threshold:
                            self.state.transition(
                                RunStatus.PAUSED,
                                reason=f"no_progress_detected: same failure repeated {streak} consecutive times"
                            )
                            self._save_state()
                            return self.state

                    case Verdict.REQUEST_REPLAN:
                        plan = await self._do_plan(
                            task_description,
                            replan_context=evaluation.replan_reason,
                        )
                        break

                    case Verdict.HALT_RUN:
                        self.state.transition(
                            RunStatus.HALTED, reason=evaluation.feedback
                        )
                        self._save_state()
                        return self.state

        self.state.transition(RunStatus.COMPLETED)
        self._save_state()
        return self.state

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_eval_progress(
        self, evaluation: EvaluationResult, task: Task, threshold_overridden: bool
    ) -> None:
        """Record an evaluation snapshot to the persistent progress file."""
        from datetime import datetime, timezone
        entry = EvaluationProgressEntry(
            iteration=self.state.iterations_on_current_task,
            task_id=task.id,
            scores=dict(evaluation.scores),
            verdict=evaluation.verdict.value,
            threshold_overridden=threshold_overridden,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._eval_progress.add(entry)
        self._eval_progress.save(self.run_dir / "evaluation_progress.json")

    def _enforce_thresholds(self, evaluation: EvaluationResult) -> EvaluationResult:
        """Override verdict to RETRY_TASK if any blocking check fails.

        Two enforcement layers:

        1. **Declared dimensions** (evaluation_dimensions in config):
           For each dimension's checks, verify pass_fail checks passed and
           metric checks meet their thresholds — using the evaluator's
           reported check_results and scores as evidence.

        2. **Legacy score_thresholds** (score_thresholds in config):
           Simple score >= threshold check. Kept for backward compat.

        Only overrides ADVANCE_TASK — never softens HALT or REPLAN.
        """
        if evaluation.verdict != Verdict.ADVANCE_TASK:
            return evaluation

        failing: list[str] = []

        # --- Layer 1: Declared dimension checks ---
        if self._dimensions:
            # Build a lookup from evaluator's check_results
            reported_checks = {cr.name: cr for cr in evaluation.check_results}

            for dim in self._dimensions:
                if dim.severity != "blocking":
                    continue
                for check in dim.checks:
                    reported = reported_checks.get(check.name)
                    if check.check_type == "pass_fail":
                        if reported is None or not reported.passed:
                            evidence = reported.output if reported else "not reported"
                            failing.append(f"{dim.name}/{check.name}: FAILED ({evidence})")
                    elif check.check_type == "metric" and check.threshold is not None:
                        actual = evaluation.scores.get(check.name)
                        if actual is None:
                            failing.append(f"{dim.name}/{check.name}: not measured (required)")
                        elif actual < check.threshold:
                            failing.append(f"{dim.name}/{check.name}: {actual:.4f} < {check.threshold} threshold")

        # --- Layer 2: Legacy score_thresholds ---
        if self.config.score_thresholds and not self._dimensions:
            for dimension, threshold in self.config.score_thresholds.items():
                actual = evaluation.scores.get(dimension)
                if actual is not None and actual < threshold:
                    failing.append(f"{dimension}: {actual:.2f} < {threshold:.2f}")

        if not failing:
            return evaluation

        from dataclasses import replace
        return replace(
            evaluation,
            verdict=Verdict.RETRY_TASK,
            feedback=(
                "Threshold enforcement: evaluator approved but checks below gates.\n"
                "Failing checks:\n"
                + "\n".join(f"  - {f}" for f in failing)
                + f"\n\nOriginal feedback: {evaluation.feedback}"
            ),
        )

    def _compute_failure_fingerprint(self, evaluation: EvaluationResult) -> str:
        """Compute a fingerprint from an evaluation to detect repeated failures."""
        failed_checks = sorted(
            cr.name for cr in evaluation.check_results if not cr.passed
        )
        feedback_hash = hashlib.md5(
            evaluation.feedback.strip().lower().encode()
        ).hexdigest()[:8]
        return f"{evaluation.verdict.value}:{','.join(failed_checks)}:{feedback_hash}"

    def _load_prior_evaluation(self) -> EvaluationResult | None:
        """Load the latest evaluation artifact for the current task, if any.

        Used on resume to restore evaluator feedback context so the
        generator receives the same structured feedback it would have
        gotten if the run hadn't been interrupted.
        """
        if self.state.iterations_on_current_task == 0:
            return None

        task_index = self.state.current_task_index
        # Scan for the task directory matching the current index
        tasks_dir = self.run_dir / "tasks"
        if not tasks_dir.exists():
            return None

        for task_dir in sorted(tasks_dir.iterdir()):
            if task_dir.name.startswith(f"{task_index}_"):
                # Find the latest evaluation artifact
                last_iter = self.state.iterations_on_current_task - 1
                eval_path = task_dir / f"evaluation_iter{last_iter}.json"
                if eval_path.exists():
                    data = json.loads(eval_path.read_text(encoding="utf-8"))
                    return EvaluationResult.from_json(data)
                break

        return None

    def _track(self, execution: StageExecution) -> None:
        """Update cumulative spend and session ID registry from a stage execution."""
        self.state.cumulative_spend_usd += execution.usage.spend_usd
        self.state.session_ids[execution.session_key] = execution.session_id
        self.state.updated_at = _utcnow()

    def _adopt_plan(self, plan: Plan) -> None:
        """Adopt a (possibly new) plan: reset task iteration state."""
        self.state.total_tasks = len(plan.tasks)
        self.state.current_task_index = 0
        self.state.iterations_on_current_task = 0
        if plan.tasks:
            self.state.current_phase = "task_0"
        else:
            self.state.current_phase = "done"
        self.state.updated_at = _utcnow()

    def _commit_checkpoint(self, task: Task) -> None:
        """Git-commit the working tree after a task passes evaluation.

        For target_repo projects, runs ``git add -A && git commit`` via
        subprocess. For greenfield projects, ensures the project directory
        exists (git init if needed) then commits.

        Failures are logged but do not abort the run.
        """
        project_path = Path(self.project_cwd)
        project_path.mkdir(parents=True, exist_ok=True)

        # Ensure a git repo exists (greenfield case)
        git_dir = project_path / ".git"
        if not git_dir.exists():
            subprocess.run(
                ["git", "init"],
                cwd=str(project_path),
                capture_output=True,
                text=True,
            )

        commit_msg = f"harness: complete {task.title}"
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg, "--allow-empty"],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            # Log the failure to the run's logs directory — non-fatal but visible
            logs_dir = self.run_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / "git_errors.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"[{_utcnow()}] git checkpoint failed for task '{task.id}': "
                    f"{exc.stderr or exc.stdout or str(exc)}\n"
                )

    def _save_state(self) -> None:
        """Persist current RunState to ``run_dir/run_state.json``."""
        state_path = self.run_dir / "run_state.json"
        self.state.save(state_path)

    def _write_artifacts(self, plan: Plan) -> None:
        """Write plan.json and plan.md to the run directory."""
        self.run_dir.mkdir(parents=True, exist_ok=True)

        plan_json_path = self.run_dir / "plan.json"
        plan_json_path.write_text(
            json.dumps(plan.to_json(), indent=2), encoding="utf-8"
        )
        self._register_artifact(str(plan_json_path))

        plan_md_path = self.run_dir / "plan.md"
        plan_md_path.write_text(plan.to_markdown(), encoding="utf-8")
        self._register_artifact(str(plan_md_path))

    def _write_task_artifacts(
        self, artifact_type: str, task: Task, result: GenerationResult | EvaluationResult
    ) -> None:
        """Write generation/evaluation artifacts to ``run_dir/tasks/<index>_<id>/``.

        Each artifact is written in both JSON and Markdown form.
        """
        task_dir = (
            self.run_dir
            / "tasks"
            / f"{self.state.current_task_index}_{task.id}"
        )
        task_dir.mkdir(parents=True, exist_ok=True)

        # Iteration suffix for multi-attempt tasks
        iteration = self.state.iterations_on_current_task
        base_name = f"{artifact_type}_iter{iteration}"

        # JSON
        json_path = task_dir / f"{base_name}.json"
        json_path.write_text(
            json.dumps(result.to_json(), indent=2), encoding="utf-8"
        )
        self._register_artifact(str(json_path))

        # Markdown
        md_path = task_dir / f"{base_name}.md"
        md_path.write_text(result.to_markdown(), encoding="utf-8")
        self._register_artifact(str(md_path))

    def _register_artifact(self, path: str) -> None:
        """Add an artifact path to the manifest if not already present."""
        if path not in self.state.artifact_manifest:
            self.state.artifact_manifest.append(path)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_project(
        cls,
        project_dir: str | Path,
        run_id: str | None = None,
        *,
        planner: Planner | None = None,
        generator: Generator | None = None,
        evaluator: Evaluator | None = None,
    ) -> Orchestrator:
        """Create an Orchestrator from a project directory.

        Loads the ``HarnessConfig``, creates a timestamped run directory
        under ``<project_dir>/runs/``, and returns a ready-to-run
        ``Orchestrator``.

        Agent implementations (planner, generator, evaluator) may be
        injected here. If omitted, they must be set before calling
        ``run()``.  Step 4 provides default SDK-backed implementations.

        Args:
            project_dir: Path to the project directory containing
                ``config.json`` and ``prompts/``.
            run_id: Optional run identifier.  Defaults to a UTC
                timestamp string.
            planner: Optional Planner implementation.
            generator: Optional Generator implementation.
            evaluator: Optional Evaluator implementation.
        """
        project_dir = Path(project_dir)
        config = HarnessConfig.from_project(project_dir)

        if run_id is None:
            run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        run_dir = project_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            planner=planner,  # type: ignore[arg-type]
            generator=generator,  # type: ignore[arg-type]
            evaluator=evaluator,  # type: ignore[arg-type]
            config=config,
            run_dir=run_dir,
        )

    @classmethod
    def resume(
        cls,
        project_dir: str | Path,
        run_id: str,
        *,
        planner: Planner | None = None,
        generator: Generator | None = None,
        evaluator: Evaluator | None = None,
    ) -> Orchestrator:
        """Resume an existing run from its persisted state.

        Loads the ``RunState`` from ``<project_dir>/runs/<run_id>/run_state.json``
        and creates an Orchestrator positioned to continue from where
        the run was paused or failed.

        Only runs in PAUSED or FAILED status can be resumed.

        Args:
            project_dir: Path to the project directory.
            run_id: The run ID to resume.
            planner: Optional Planner implementation.
            generator: Optional Generator implementation.
            evaluator: Optional Evaluator implementation.

        Raises:
            FileNotFoundError: If the run directory or state file is missing.
            ValueError: If the run is not in a resumable state.
        """
        project_dir = Path(project_dir)
        config = HarnessConfig.from_project(project_dir)

        run_dir = project_dir / "runs" / run_id
        state_path = run_dir / "run_state.json"

        if not state_path.exists():
            raise FileNotFoundError(
                f"run_state.json not found for run '{run_id}' at {state_path}"
            )

        state = RunState.load(state_path)

        resumable = {RunStatus.PAUSED, RunStatus.FAILED}
        if state.status not in resumable:
            raise ValueError(
                f"Run '{run_id}' is in status '{state.status.value}' "
                f"and cannot be resumed. Only PAUSED or FAILED runs "
                f"are resumable."
            )

        orch = cls(
            planner=planner,  # type: ignore[arg-type]
            generator=generator,  # type: ignore[arg-type]
            evaluator=evaluator,  # type: ignore[arg-type]
            config=config,
            run_dir=run_dir,
        )
        orch.state = state
        return orch


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
