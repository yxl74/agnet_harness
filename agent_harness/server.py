"""FastAPI web server for the Agent Harness.

Exposes a REST API for project/run management and a WebSocket endpoint for
AI-assisted project configuration. Serves a single-page HTML UI from
``static/index.html``.

Usage (via CLI):
    python -m agent_harness serve [--host 127.0.0.1] [--port 8000]

Direct (uvicorn):
    uvicorn agent_harness.server:app --reload
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Agent Harness", version="0.1.0")

_STATIC_DIR = Path(__file__).parent / "static"

# Serve static assets (CSS, JS, etc.) if present
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _projects_dir() -> Path:
    """Return the projects directory, configurable via HARNESS_PROJECTS_DIR env var."""
    env_dir = os.environ.get("HARNESS_PROJECTS_DIR")
    if env_dir:
        return Path(env_dir)
    return Path("projects")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    task: str
    run_id: str | None = None


# ---------------------------------------------------------------------------
# Route: serve SPA root
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Route: list projects
# ---------------------------------------------------------------------------


@app.get("/api/projects")
async def list_projects() -> JSONResponse:
    """List all project directories that have a config.json, with run counts."""
    projects_dir = _projects_dir()
    if not projects_dir.exists():
        return JSONResponse(content=[])

    results: list[dict[str, Any]] = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue
        config_path = entry / "config.json"
        if not config_path.exists():
            continue

        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            config = {}

        runs_dir = entry / "runs"
        runs_count = 0
        if runs_dir.exists():
            runs_count = sum(1 for r in runs_dir.iterdir() if r.is_dir())

        results.append(
            {
                "name": entry.name,
                "config": config,
                "runs_count": runs_count,
            }
        )

    return JSONResponse(content=results)


# ---------------------------------------------------------------------------
# Route: list runs for a project
# ---------------------------------------------------------------------------


@app.get("/api/projects/{name}/runs")
async def list_runs(name: str) -> JSONResponse:
    """List all runs for a project with their status and summary."""
    projects_dir = _projects_dir()
    project_dir = projects_dir / name

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found.")

    runs_dir = project_dir / "runs"
    if not runs_dir.exists():
        return JSONResponse(content=[])

    runs: list[dict[str, Any]] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        state_path = run_dir / "run_state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {"status": "unknown"}
        else:
            state = {"status": "unknown"}

        runs.append(
            {
                "run_id": run_dir.name,
                "status": state.get("status", "unknown"),
                "current_phase": state.get("current_phase"),
                "total_tasks": state.get("total_tasks", 0),
                "completed_tasks": len(state.get("completed_task_ids", [])),
                "cumulative_spend_usd": state.get("cumulative_spend_usd", 0.0),
                "started_at": state.get("started_at"),
                "updated_at": state.get("updated_at"),
            }
        )

    return JSONResponse(content=runs)


# ---------------------------------------------------------------------------
# Route: get a specific run's state
# ---------------------------------------------------------------------------


@app.get("/api/projects/{name}/runs/{run_id}/state")
async def get_run_state(name: str, run_id: str) -> JSONResponse:
    """Return the raw run_state.json for a specific run."""
    projects_dir = _projects_dir()
    state_path = projects_dir / name / "runs" / run_id / "run_state.json"

    if not state_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' not found for project '{name}'.",
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to read run state: {exc}"
        ) from exc

    return JSONResponse(content=state)


# ---------------------------------------------------------------------------
# Route: start a new run
# ---------------------------------------------------------------------------


async def _run_orchestrator(
    project_name: str, task: str, run_id: str, projects_dir: Path
) -> None:
    """Background task: run the harness orchestrator for a project."""
    project_dir = projects_dir / project_name

    try:
        # Lazy imports so the SDK is optional at module import time
        from agent_harness.config import HarnessConfig
        from agent_harness.core import Orchestrator

        try:
            from agent_harness.agents import (  # type: ignore[import]
                DefaultEvaluator,
                DefaultGenerator,
                DefaultPlanner,
            )
            config = HarnessConfig.from_project(project_dir)
            planner = DefaultPlanner(config)
            generator = DefaultGenerator(config)
            evaluator = DefaultEvaluator(config)
        except ImportError:
            planner = None  # type: ignore[assignment]
            generator = None  # type: ignore[assignment]
            evaluator = None  # type: ignore[assignment]

        orchestrator = Orchestrator.from_project(
            project_dir,
            run_id=run_id,
            planner=planner,
            generator=generator,
            evaluator=evaluator,
        )
        await orchestrator.run(task)

    except Exception:  # noqa: BLE001
        # Persist a FAILED state so the UI can surface the error
        run_dir = projects_dir / project_name / "runs" / run_id
        state_path = run_dir / "run_state.json"
        if not state_path.exists():
            run_dir.mkdir(parents=True, exist_ok=True)
            import traceback

            err_payload = {
                "run_id": run_id,
                "status": "failed",
                "status_reason": traceback.format_exc(),
                "current_phase": "planning",
                "current_task_index": 0,
                "total_tasks": 0,
                "iterations_on_current_task": 0,
                "plan_version": 0,
                "completed_task_ids": [],
                "cumulative_spend_usd": 0.0,
                "session_ids": {},
                "artifact_manifest": [],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            state_path.write_text(
                json.dumps(err_payload, indent=2), encoding="utf-8"
            )


@app.post("/api/projects/{name}/run")
async def start_run(name: str, request: RunRequest) -> JSONResponse:
    """Start a new harness run in the background and return the run_id immediately."""
    projects_dir = _projects_dir()
    project_dir = projects_dir / name

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found.")

    # Determine run_id
    run_id = request.run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Make sure the run dir is unique to avoid collisions
    run_dir = project_dir / "runs" / run_id
    if run_dir.exists():
        raise HTTPException(
            status_code=409, detail=f"Run '{run_id}' already exists."
        )
    run_dir.mkdir(parents=True, exist_ok=True)

    asyncio.create_task(
        _run_orchestrator(name, request.task, run_id, projects_dir)
    )

    return JSONResponse(
        status_code=202,
        content={"run_id": run_id, "status": "accepted"},
    )


# ---------------------------------------------------------------------------
# WebSocket: AI-assisted project configurator
# ---------------------------------------------------------------------------


@app.websocket("/ws/configure")
async def configure_project(websocket: WebSocket) -> None:
    """WebSocket endpoint for AI-assisted project configuration.

    Protocol (JSON messages):
      Client -> Server:  {"type": "configure", "description": "<free-form description>"}
      Server -> Client:  {"type": "message",   "text": "<progress text>"}
      Server -> Client:  {"type": "done",       "project_name": "<name>"}
      Server -> Client:  {"type": "error",      "text": "<error message>"}
    """
    await websocket.accept()

    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
    except (WebSocketDisconnect, json.JSONDecodeError) as exc:
        await websocket.close(code=1003)
        return

    if msg.get("type") != "configure" or not msg.get("description"):
        await websocket.send_json(
            {"type": "error", "text": "Expected {type: 'configure', description: '...'}"}
        )
        await websocket.close(code=1003)
        return

    description: str = msg["description"]

    # Try to use the SDK; fall back to a stub configurator if unavailable
    try:
        import claude_agent_sdk as sdk  # type: ignore[import]
        sdk_available = True
    except ImportError:
        sdk_available = False

    if not sdk_available:
        await websocket.send_json(
            {
                "type": "message",
                "text": (
                    "claude-agent-sdk is not installed. "
                    "Falling back to a simple scaffold configurator."
                ),
            }
        )
        project_name = await _stub_configure(websocket, description)
        if project_name:
            await websocket.send_json({"type": "done", "project_name": project_name})
        await websocket.close()
        return

    # SDK is available — use it to interview the user and write project files
    await websocket.send_json(
        {"type": "message", "text": "Starting AI configurator..."}
    )

    try:
        project_name = await _sdk_configure(websocket, description, sdk)
        await websocket.send_json({"type": "done", "project_name": project_name})
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        await websocket.send_json({"type": "error", "text": str(exc)})
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


async def _stub_configure(websocket: WebSocket, description: str) -> str | None:
    """Simple fallback configurator that scaffolds a project without the SDK."""
    from agent_harness.config import HarnessConfig

    # Derive a project name from the description
    import re
    project_name = re.sub(r"[^a-z0-9]+", "-", description.lower().strip())[:40].strip("-")
    if not project_name:
        project_name = "new-project"

    await websocket.send_json(
        {"type": "message", "text": f"Creating project '{project_name}'..."}
    )

    projects_dir = _projects_dir()
    project_dir = projects_dir / project_name
    HarnessConfig.scaffold_project(project_dir, name=project_name)

    await websocket.send_json(
        {"type": "message", "text": f"Project scaffolded at projects/{project_name}"}
    )
    return project_name


async def _sdk_configure(
    websocket: WebSocket, description: str, sdk: Any
) -> str:
    """Use claude-agent-sdk to interview the user and generate project files."""
    projects_dir = _projects_dir()

    system_prompt = (
        "You are an AI assistant that helps configure an Agent Harness project. "
        "The user will describe a task or project they want to build. "
        "You should:\n"
        "1. Ask any clarifying questions needed to understand the project requirements.\n"
        "2. Generate a project name (lowercase, hyphenated).\n"
        "3. Create a config.json with appropriate settings.\n"
        "4. Create planner.md, generator.md, and evaluator.md prompts.\n"
        "5. Write these files to the projects/<name>/ directory.\n\n"
        f"The projects directory is at: {projects_dir.resolve()}\n\n"
        "When you are done, output a line that starts with 'PROJECT_NAME:' "
        "followed by the project name you created."
    )

    user_message = f"I want to create an agent harness project for: {description}"

    collected_output: list[str] = []

    async for event in sdk.query(
        prompt=user_message,
        system=system_prompt,
        tools=["Write", "Read", "Bash"],
    ):
        # Stream progress back over the WebSocket
        if hasattr(event, "type"):
            if event.type == "text":
                text: str = getattr(event, "text", "")
                if text:
                    collected_output.append(text)
                    await websocket.send_json({"type": "message", "text": text})
            elif event.type == "tool_use":
                tool_name = getattr(event, "name", "tool")
                await websocket.send_json(
                    {"type": "message", "text": f"[Using tool: {tool_name}]"}
                )

    # Extract project name from SDK output
    import re
    full_output = "\n".join(collected_output)
    match = re.search(r"PROJECT_NAME:\s*(\S+)", full_output)
    if match:
        return match.group(1)

    # Fallback: derive from description
    project_name = re.sub(r"[^a-z0-9]+", "-", description.lower().strip())[:40].strip("-")
    return project_name or "new-project"
