"""HarnessConfig: load and scaffold agent harness project configurations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Default prompt templates written when scaffolding a new project
# ---------------------------------------------------------------------------

_DEFAULT_PLANNER_PROMPT = """\
You are the Planner agent in an agent harness.

Your job is to take a high-level task description and produce a structured plan:
- A brief spec / product context
- An ordered list of tasks, each with a clear title, description, acceptance criteria,
  dependencies on prior tasks, and a contract (success_criteria + scope_boundaries)
- Overall acceptance criteria for the entire run

Keep tasks small and independently verifiable. Write the plan as `plan.md` in the run
directory and return structured JSON in `plan.json`.
"""

_DEFAULT_GENERATOR_PROMPT = """\
You are the Generator agent in an agent harness.

Your job is to implement the current task as defined by its contract:
- Read the contract carefully — success_criteria and scope_boundaries are the definition of "done"
- Modify the working tree to satisfy all success criteria
- Do NOT commit to git; the orchestrator owns commits after evaluation passes
- Write a human-readable summary of what you did to `generation.md`

If you are retrying after an evaluation, the prior EvaluationResult is provided — use the
structured check_results and scores to target your repairs precisely.
"""

_DEFAULT_EVALUATOR_PROMPT = """\
You are the Evaluator agent in an agent harness.

Your job is to evaluate the generator's work against the task contract:
1. Run deterministic checks: tests, linter, type checker
2. Perform an AI review of code quality, spec adherence, and security
3. Aggregate results into per-criterion scores
4. Emit exactly one verdict:
   - ADVANCE_TASK   — all blocking checks pass and scores meet thresholds
   - RETRY_TASK     — task needs rework; provide actionable, structured feedback
   - REQUEST_REPLAN — the contract itself is flawed; explain why in replan_reason
   - HALT_RUN       — quality is irrecoverable or further iteration is futile

Write a human-readable report to `evaluation.md` and machine-readable results to
`evaluation.json`.
"""

_DEFAULT_CONFIG: dict = {
    "name": "my-project",
    "model": "claude-opus-4-6",
    "max_budget_usd": 10.0,
    "generator_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    "evaluator_tools": ["Read", "Bash", "Glob", "Grep"],
    "planner_tools": ["Read", "Glob", "Grep"],
    "target_repo": None,
    "session_mode": "long_lived",
}


# ---------------------------------------------------------------------------
# HarnessConfig dataclass
# ---------------------------------------------------------------------------


@dataclass
class HarnessConfig:
    name: str
    model: str                       # e.g., "claude-opus-4-6"
    max_budget_usd: float
    generator_tools: list[str]
    evaluator_tools: list[str]
    planner_tools: list[str]
    target_repo: str | None          # Path to existing repo, or None for greenfield
    session_mode: str                # "long_lived" or "fresh"
    planner_prompt: str              # System prompt content (loaded from .md file)
    generator_prompt: str
    evaluator_prompt: str

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def from_project(cls, project_dir: str | Path) -> HarnessConfig:
        """Load a HarnessConfig from a project directory.

        Reads ``config.json`` and the three prompt files under ``prompts/``.
        Missing prompt files fall back to the built-in defaults so the config
        always has usable content.

        Raises:
            FileNotFoundError: if ``config.json`` does not exist.
            json.JSONDecodeError: if ``config.json`` is not valid JSON.
        """
        project_dir = Path(project_dir)
        config_path = project_dir / "config.json"

        if not config_path.exists():
            raise FileNotFoundError(
                f"config.json not found in project directory: {project_dir}"
            )

        raw = json.loads(config_path.read_text(encoding="utf-8"))

        prompts_dir = project_dir / "prompts"

        def _load_prompt(filename: str, default: str) -> str:
            path = prompts_dir / filename
            if path.exists():
                return path.read_text(encoding="utf-8")
            return default

        return cls(
            name=raw.get("name", "unnamed"),
            model=raw.get("model", "claude-opus-4-6"),
            max_budget_usd=float(raw.get("max_budget_usd", 10.0)),
            generator_tools=list(raw.get("generator_tools", _DEFAULT_CONFIG["generator_tools"])),
            evaluator_tools=list(raw.get("evaluator_tools", _DEFAULT_CONFIG["evaluator_tools"])),
            planner_tools=list(raw.get("planner_tools", _DEFAULT_CONFIG["planner_tools"])),
            target_repo=raw.get("target_repo"),
            session_mode=raw.get("session_mode", "long_lived"),
            planner_prompt=_load_prompt("planner.md", _DEFAULT_PLANNER_PROMPT),
            generator_prompt=_load_prompt("generator.md", _DEFAULT_GENERATOR_PROMPT),
            evaluator_prompt=_load_prompt("evaluator.md", _DEFAULT_EVALUATOR_PROMPT),
        )

    # ------------------------------------------------------------------
    # Scaffolding
    # ------------------------------------------------------------------

    @staticmethod
    def scaffold_project(project_dir: str | Path, name: str) -> None:
        """Create a minimal project directory structure with default config and prompts.

        Creates:
            <project_dir>/
                config.json
                prompts/
                    planner.md
                    generator.md
                    evaluator.md
                runs/

        If the directory already exists the call is idempotent: existing files
        are NOT overwritten, new files are added.
        """
        project_dir = Path(project_dir)
        project_dir.mkdir(parents=True, exist_ok=True)

        # config.json
        config_path = project_dir / "config.json"
        if not config_path.exists():
            config = dict(_DEFAULT_CONFIG)
            config["name"] = name
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        # prompts/
        prompts_dir = project_dir / "prompts"
        prompts_dir.mkdir(exist_ok=True)

        defaults = {
            "planner.md": _DEFAULT_PLANNER_PROMPT,
            "generator.md": _DEFAULT_GENERATOR_PROMPT,
            "evaluator.md": _DEFAULT_EVALUATOR_PROMPT,
        }
        for filename, content in defaults.items():
            prompt_path = prompts_dir / filename
            if not prompt_path.exists():
                prompt_path.write_text(content, encoding="utf-8")

        # runs/
        runs_dir = project_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
