"""Core orchestrator and protocol definitions for the agent harness."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent_harness.artifacts import (
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
        self.project_cwd = (
            str(Path(config.target_repo).resolve())
            if config.target_repo
            else str(self.run_dir / "project")
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, task_description: str) -> RunState:
        """Execute the full harness loop and return the final RunState."""

        # ---- Phase 1: Plan ----
        plan_exec = await self.planner.plan(task_description, cwd=self.project_cwd)
        plan = plan_exec.result
        self._track(plan_exec)
        self._adopt_plan(plan)
        self._write_artifacts(plan)
        self.state.transition(RunStatus.EXECUTING)
        self._save_state()

        # ---- Phase 2: Per-task loop ----
        while self.state.current_task_index < len(plan.tasks):
            task = plan.tasks[self.state.current_task_index]

            # Skip already-completed tasks (from a prior plan version)
            if task.id in self.state.completed_task_ids:
                self.state.advance_task()
                self._save_state()
                continue

            prior_evaluation: EvaluationResult | None = None

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
                self._write_task_artifacts("evaluation", task, evaluation)
                self.state.iterations_on_current_task += 1
                self._save_state()

                match evaluation.verdict:
                    case Verdict.ADVANCE_TASK:
                        self._commit_checkpoint(task)
                        self.state.completed_task_ids.append(task.id)
                        self.state.advance_task()
                        self._save_state()
                        break

                    case Verdict.RETRY_TASK:
                        prior_evaluation = evaluation
                        # No-progress detection would go here

                    case Verdict.REQUEST_REPLAN:
                        plan_exec = await self.planner.plan(
                            task_description,
                            cwd=self.project_cwd,
                            replan_context=evaluation.replan_reason,
                            completed_task_ids=self.state.completed_task_ids,
                        )
                        plan = plan_exec.result
                        self._track(plan_exec)
                        self.state.plan_version += 1
                        self._adopt_plan(plan)
                        self._write_artifacts(plan)
                        self.state.transition(RunStatus.EXECUTING)
                        self._save_state()
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
        except subprocess.CalledProcessError:
            # Commit failures (e.g., nothing to commit) are non-fatal
            pass

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
