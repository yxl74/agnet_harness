"""CLI entry point for the agent harness.

Usage:
    python -m agent_harness run <project> "task description"
    python -m agent_harness resume <project> <run-id>
    python -m agent_harness new <project-name>
    python -m agent_harness list
    python -m agent_harness serve
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _projects_dir() -> Path:
    """Return the projects/ directory relative to cwd."""
    return Path.cwd() / "projects"


def _project_dir(project_name: str) -> Path:
    return _projects_dir() / project_name


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Run the harness for a project with a task description."""
    import anyio

    project_name: str = args.project
    task_description: str = args.task

    project_dir = _project_dir(project_name)
    if not project_dir.exists():
        print(f"Error: project '{project_name}' not found at {project_dir}", file=sys.stderr)
        print(f"Tip: run 'python -m agent_harness new {project_name}' to scaffold it.", file=sys.stderr)
        return 1

    async def _run() -> int:
        # Lazy imports so claude-agent-sdk is not required at import time
        from agent_harness.config import HarnessConfig
        from agent_harness.core import Orchestrator

        try:
            from agent_harness.planner import DefaultPlanner
            from agent_harness.generator import DefaultGenerator
            from agent_harness.evaluator import DefaultEvaluator
            _has_agents = True
        except ImportError:
            _has_agents = False

        print(f"Loading project '{project_name}' from {project_dir} ...")

        try:
            config = HarnessConfig.from_project(project_dir)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error loading project config: {exc}", file=sys.stderr)
            return 1

        print(f"Planning task: {task_description!r}")

        # Build agent instances from config
        if _has_agents:
            cwd = str(Path(config.target_repo).resolve()) if config.target_repo else str(project_dir)
            planner = DefaultPlanner(
                system_prompt=config.planner_prompt,
                tools=config.planner_tools,
                model=config.model,
                cwd=cwd,
                structured_output=config.structured_output,
                effort=config.get_effort("planner"),
            )
            generator = DefaultGenerator(
                system_prompt=config.generator_prompt,
                tools=config.generator_tools,
                model=config.model,
                cwd=cwd,
                effort=config.get_effort("generator"),
            )
            evaluator = DefaultEvaluator(
                system_prompt=config.evaluator_prompt,
                tools=config.evaluator_tools,
                model=config.model,
                cwd=cwd,
                structured_output=config.structured_output,
                evaluation_dimensions=config.evaluation_dimensions,
                mcp_servers=config.evaluator_mcp_servers,
                effort=config.get_effort("evaluator"),
            )
        else:
            planner = None  # type: ignore[assignment]
            generator = None  # type: ignore[assignment]
            evaluator = None  # type: ignore[assignment]

        if planner is None or generator is None or evaluator is None:
            print(
                "Warning: Default agent implementations not available "
                "(claude-agent-sdk may not be installed). "
                "Orchestrator will raise if run() is called without agents.",
                file=sys.stderr,
            )

        orchestrator = Orchestrator.from_project(
            project_dir,
            planner=planner,
            generator=generator,
            evaluator=evaluator,
        )

        try:
            print(f"Starting run {orchestrator.run_dir.name} ...")
            state = await orchestrator.run(task_description)
        except KeyboardInterrupt:
            print("\nInterrupted. Saving state ...", file=sys.stderr)
            orchestrator._save_state()
            state = orchestrator.state
            print(f"Run paused. Resume with: python -m agent_harness resume {project_name} {orchestrator.run_dir.name}")
            return 130

        print(f"\nRun complete.")
        print(f"  Status:        {state.status.value}")
        print(f"  Tasks done:    {len(state.completed_task_ids)} / {state.total_tasks}")
        print(f"  Spend:         ${state.cumulative_spend_usd:.4f}")
        if state.status_reason:
            print(f"  Reason:        {state.status_reason}")
        print(f"  Artifacts in:  {orchestrator.run_dir}")
        return 0

    return anyio.run(_run)


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume a paused or failed run."""
    import anyio

    project_name: str = args.project
    run_id: str = args.run_id

    project_dir = _project_dir(project_name)
    if not project_dir.exists():
        print(f"Error: project '{project_name}' not found at {project_dir}", file=sys.stderr)
        return 1

    async def _resume() -> int:
        from agent_harness.core import Orchestrator

        try:
            from agent_harness.planner import DefaultPlanner
            from agent_harness.generator import DefaultGenerator
            from agent_harness.evaluator import DefaultEvaluator
            from agent_harness.config import HarnessConfig
            config = HarnessConfig.from_project(project_dir)
            cwd = str(Path(config.target_repo).resolve()) if config.target_repo else str(project_dir)
            planner = DefaultPlanner(
                system_prompt=config.planner_prompt,
                tools=config.planner_tools,
                model=config.model,
                cwd=cwd,
                structured_output=config.structured_output,
                effort=config.get_effort("planner"),
            )
            generator = DefaultGenerator(
                system_prompt=config.generator_prompt,
                tools=config.generator_tools,
                model=config.model,
                cwd=cwd,
                effort=config.get_effort("generator"),
            )
            evaluator = DefaultEvaluator(
                system_prompt=config.evaluator_prompt,
                tools=config.evaluator_tools,
                model=config.model,
                cwd=cwd,
                structured_output=config.structured_output,
                evaluation_dimensions=config.evaluation_dimensions,
                mcp_servers=config.evaluator_mcp_servers,
                effort=config.get_effort("evaluator"),
            )
        except ImportError:
            planner = None  # type: ignore[assignment]
            generator = None  # type: ignore[assignment]
            evaluator = None  # type: ignore[assignment]

        try:
            orchestrator = Orchestrator.resume(
                project_dir,
                run_id,
                planner=planner,
                generator=generator,
                evaluator=evaluator,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error resuming run: {exc}", file=sys.stderr)
            return 1

        print(f"Resuming run {run_id} for project '{project_name}' ...")
        print(f"  Current status: {orchestrator.state.status.value}")
        print(f"  Tasks done:     {len(orchestrator.state.completed_task_ids)} / {orchestrator.state.total_tasks}")

        try:
            # Re-run from the persisted task description (stored in plan artifacts)
            # We need the original task; read it from plan.json if available
            plan_json = orchestrator.run_dir / "plan.json"
            if plan_json.exists():
                import json as _json
                plan_data = _json.loads(plan_json.read_text(encoding="utf-8"))
                task_description = plan_data.get("task_description", "")
            else:
                task_description = ""

            state = await orchestrator.resume_run(task_description)
        except KeyboardInterrupt:
            print("\nInterrupted. Saving state ...", file=sys.stderr)
            orchestrator._save_state()
            state = orchestrator.state
            print(f"Run paused again. Resume with: python -m agent_harness resume {project_name} {run_id}")
            return 130

        print(f"\nRun complete.")
        print(f"  Status:     {state.status.value}")
        print(f"  Tasks done: {len(state.completed_task_ids)} / {state.total_tasks}")
        print(f"  Spend:      ${state.cumulative_spend_usd:.4f}")
        if state.status_reason:
            print(f"  Reason:     {state.status_reason}")
        return 0

    return anyio.run(_resume)


def cmd_new(args: argparse.Namespace) -> int:
    """Scaffold a new project directory."""
    from agent_harness.config import HarnessConfig

    project_name: str = args.project_name
    project_dir = _project_dir(project_name)

    if project_dir.exists():
        print(f"Project '{project_name}' already exists at {project_dir}")
        print("Existing files were not overwritten (scaffold is idempotent).")
    else:
        print(f"Creating project '{project_name}' ...")

    HarnessConfig.scaffold_project(project_dir, name=project_name)

    print(f"Project scaffolded at: {project_dir}")
    print(f"  {project_dir}/config.json       — harness settings")
    print(f"  {project_dir}/prompts/           — agent system prompts")
    print(f"    planner.md, generator.md, evaluator.md")
    print(f"  {project_dir}/runs/              — run artifacts (auto-populated)")
    print()
    print(f"Next: edit {project_dir}/config.json, then run:")
    print(f"  python -m agent_harness run {project_name} \"your task description\"")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List available projects and their active runs."""
    projects_dir = _projects_dir()

    if not projects_dir.exists():
        print(f"No projects/ directory found at {projects_dir}.")
        print("Create a project with: python -m agent_harness new <project-name>")
        return 0

    project_dirs = sorted(
        p for p in projects_dir.iterdir() if p.is_dir() and (p / "config.json").exists()
    )

    if not project_dirs:
        print("No projects found.")
        print("Create one with: python -m agent_harness new <project-name>")
        return 0

    print(f"Projects in {projects_dir}:\n")

    for proj in project_dirs:
        print(f"  {proj.name}")

        runs_dir = proj / "runs"
        if runs_dir.exists():
            run_dirs = sorted(
                r for r in runs_dir.iterdir() if r.is_dir()
            )
            if run_dirs:
                for run_dir in run_dirs:
                    state_path = run_dir / "run_state.json"
                    if state_path.exists():
                        try:
                            import json as _json
                            data = _json.loads(state_path.read_text(encoding="utf-8"))
                            status = data.get("status", "unknown")
                            spend = data.get("cumulative_spend_usd", 0.0)
                            tasks_done = len(data.get("completed_task_ids", []))
                            total = data.get("total_tasks", 0)
                            print(f"    run: {run_dir.name}  status={status}  tasks={tasks_done}/{total}  spend=${spend:.4f}")
                        except Exception:
                            print(f"    run: {run_dir.name}  (state unreadable)")
                    else:
                        print(f"    run: {run_dir.name}  (no state)")
            else:
                print("    (no runs yet)")
        else:
            print("    (no runs yet)")

    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Launch the web UI."""
    host: str = getattr(args, "host", "127.0.0.1")
    port: int = getattr(args, "port", 8000)

    try:
        import importlib
        server_mod = importlib.import_module("agent_harness.server")
        app = getattr(server_mod, "app", None)
        if app is None:
            print("Error: agent_harness.server does not expose an 'app' object.", file=sys.stderr)
            return 1
    except ImportError:
        print("Web UI not yet implemented (agent_harness/server.py not found).")
        print("Implement Step 6 to enable the web UI.")
        return 0

    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        return 1

    print(f"Starting web UI at http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_harness",
        description="Agent harness: orchestrate Planner, Generator, and Evaluator agents.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    # -- run --
    run_parser = subparsers.add_parser(
        "run",
        help="Run the harness for a project.",
        description="Load a project config and run the harness loop for the given task.",
    )
    run_parser.add_argument("project", help="Project name (subdirectory of projects/).")
    run_parser.add_argument("task", help="Task description (quoted string).")

    # -- resume --
    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a paused or failed run.",
        description="Resume an existing run from its persisted state.",
    )
    resume_parser.add_argument("project", help="Project name.")
    resume_parser.add_argument("run_id", help="Run ID (directory name under projects/<project>/runs/).")

    # -- new --
    new_parser = subparsers.add_parser(
        "new",
        help="Scaffold a new project.",
        description="Create a new project directory with default config and prompts.",
    )
    new_parser.add_argument("project_name", help="Name for the new project.")

    # -- list --
    subparsers.add_parser(
        "list",
        help="List available projects and their runs.",
        description="Scan the projects/ directory and show each project with its run history.",
    )

    # -- serve --
    serve_parser = subparsers.add_parser(
        "serve",
        help="Launch the web UI.",
        description="Start the FastAPI web interface (requires Step 6 / server.py).",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    serve_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    dispatch = {
        "run": cmd_run,
        "resume": cmd_resume,
        "new": cmd_new,
        "list": cmd_list,
        "serve": cmd_serve,
    }

    handler = dispatch[args.command]
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
