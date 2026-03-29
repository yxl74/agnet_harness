"""HarnessConfig: load and scaffold agent harness project configurations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate bundled template files
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates" / "default"


def _read_template(relative_path: str) -> str:
    """Read a file from the bundled default template directory."""
    return (_TEMPLATES_DIR / relative_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lazy-loaded defaults (read from disk only when first needed)
# ---------------------------------------------------------------------------

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
    session_mode: str                # Reserved for future use. Generator is always long-lived-per-task, evaluator always fresh.
    planner_prompt: str              # System prompt content (loaded from .md file)
    generator_prompt: str
    evaluator_prompt: str
    structured_output: bool = True   # Use schema-validated structured output (recommended)
    max_retries_per_task: int = 5         # Hard ceiling on attempts per task
    no_progress_threshold: int = 3        # Pause if same failure repeats this many times
    score_thresholds: dict[str, float] | None = None  # Hard gates: {"correctness": 0.8, "quality": 0.7}
    evaluator_mcp_servers: dict | None = None  # Project-specific MCP servers for evaluation
    effort: str = "high"                       # Reasoning effort: "low", "medium", "high", "max"
    evaluation_dimensions: list[dict] | None = None  # Declared dimensions with thresholds and descriptions

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

        def _load_prompt(filename: str) -> str:
            path = prompts_dir / filename
            if path.exists():
                return path.read_text(encoding="utf-8")
            # Fall back to the bundled template
            return _read_template(f"prompts/{filename}")

        return cls(
            name=raw.get("name", "unnamed"),
            model=raw.get("model", "claude-opus-4-6"),
            max_budget_usd=float(raw.get("max_budget_usd", 10.0)),
            generator_tools=list(raw.get("generator_tools", _DEFAULT_CONFIG["generator_tools"])),
            evaluator_tools=list(raw.get("evaluator_tools", _DEFAULT_CONFIG["evaluator_tools"])),
            planner_tools=list(raw.get("planner_tools", _DEFAULT_CONFIG["planner_tools"])),
            target_repo=raw.get("target_repo"),
            session_mode=raw.get("session_mode", "long_lived"),
            planner_prompt=_load_prompt("planner.md"),
            generator_prompt=_load_prompt("generator.md"),
            evaluator_prompt=_load_prompt("evaluator.md"),
            structured_output=bool(raw.get("structured_output", True)),
            max_retries_per_task=int(raw.get("max_retries_per_task", 5)),
            no_progress_threshold=int(raw.get("no_progress_threshold", 3)),
            score_thresholds=raw.get("score_thresholds"),
            evaluator_mcp_servers=raw.get("evaluator_mcp_servers"),
            effort=raw.get("effort", "high"),
            evaluation_dimensions=raw.get("evaluation_dimensions"),
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

        # config.json — start from the bundled template, then override name
        config_path = project_dir / "config.json"
        if not config_path.exists():
            config = json.loads(_read_template("config.json"))
            config["name"] = name
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        # prompts/ — copy from the bundled default templates
        prompts_dir = project_dir / "prompts"
        prompts_dir.mkdir(exist_ok=True)

        for filename in ("planner.md", "generator.md", "evaluator.md"):
            prompt_path = prompts_dir / filename
            if not prompt_path.exists():
                content = _read_template(f"prompts/{filename}")
                prompt_path.write_text(content, encoding="utf-8")

        # runs/
        runs_dir = project_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
