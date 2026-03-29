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

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest, WebSocket, WebSocketDisconnect
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
# Route: get project config (for pipeline visualization)
# ---------------------------------------------------------------------------


@app.get("/api/projects/{name}/config")
async def get_project_config(name: str) -> JSONResponse:
    """Return the full project config, prompts, and evaluation dimensions."""
    projects_dir = _projects_dir()
    project_dir = projects_dir / name
    config_path = project_dir / "config.json"

    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' config not found.")

    config = json.loads(config_path.read_text(encoding="utf-8"))

    # Load full prompts
    prompts: dict[str, str] = {}
    prompts_dir = project_dir / "prompts"
    if prompts_dir.exists():
        for md_file in prompts_dir.glob("*.md"):
            prompts[md_file.stem] = md_file.read_text(encoding="utf-8")

    return JSONResponse(content={"config": config, "prompts": prompts})


@app.patch("/api/projects/{name}/config")
async def update_project_config(name: str, request: FastAPIRequest) -> JSONResponse:
    """Update specific fields in a project's config.json."""
    projects_dir = _projects_dir()
    config_path = projects_dir / name / "config.json"

    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' config not found.")

    body = await request.json()

    # Load existing config
    config = json.loads(config_path.read_text(encoding="utf-8"))

    # Whitelist of updatable fields
    updatable = {
        "effort", "planner_effort", "generator_effort", "evaluator_effort",
        "model", "max_budget_usd", "max_retries_per_task", "no_progress_threshold",
        "structured_output", "target_repo",
    }

    updated = []
    for key, value in body.items():
        if key in updatable:
            config[key] = value
            updated.append(key)

    if not updated:
        raise HTTPException(status_code=400, detail=f"No updatable fields provided. Allowed: {sorted(updatable)}")

    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return JSONResponse(content={"updated": updated, "config": config})


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
      Client -> Server:  {"type": "configure", "description": "...", "target_repo": "/path/to/repo" (optional)}
      Client -> Server:  {"type": "reply", "text": "user's follow-up answer"}
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
    target_repo: str | None = msg.get("target_repo")

    # Validate target_repo if provided
    if target_repo:
        repo_path = Path(target_repo)
        if not repo_path.exists():
            await websocket.send_json({
                "type": "error",
                "text": f"Target repo path does not exist: {target_repo}",
            })
            await websocket.close(code=1003)
            return
        if not repo_path.is_dir():
            await websocket.send_json({
                "type": "error",
                "text": f"Target repo path is not a directory: {target_repo}",
            })
            await websocket.close(code=1003)
            return
        target_repo = str(repo_path.resolve())  # Normalize to absolute

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
        {"type": "message", "text": "Starting AI configurator — I'll walk you through setting up your project step by step."}
    )

    try:
        raw_name = await _sdk_configure(websocket, description, sdk, target_repo=target_repo)

        # --- Verify the project actually exists ---
        # The name from the agent may not match the actual directory.
        # Strategy: check the exact name first, then scan for recently
        # created projects, then fall back to scaffold.
        projects_dir = _projects_dir()
        project_name = _find_or_create_project(
            projects_dir, raw_name, description, target_repo, websocket
        )
        await websocket.send_json({"type": "done", "project_name": project_name})
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "error", "text": str(exc)})
        except Exception:
            pass
    finally:
        # Small delay so the frontend processes 'done' before 'close'
        import asyncio
        await asyncio.sleep(0.2)
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


_CONFIGURATOR_SYSTEM_PROMPT = """\
You are an expert AI configurator for the Agent Harness framework. Your job is
to interview the user about their project and progressively build a complete
project configuration — config.json and system prompts for the planner,
generator, and evaluator agents.

## Interview Process

Work through these sections IN ORDER. For each section, ask questions, discuss
trade-offs with the user, then fill in that part of the config. Do NOT try to
generate everything at once — this is a conversation.

### 0. Codebase Exploration (if target_repo provided)
BEFORE asking any questions, explore the existing codebase:
- Use Glob to understand the project structure (directories, key files)
- Use Read to examine entry points, config files, test suites, README
- Use Grep to find patterns (model definitions, data loading, evaluation scripts)
- Build a mental model of: tech stack, architecture, data flow, test coverage
- Summarize what you found to the user: "I've explored your codebase. Here's
  what I see: [summary]. Let me ask some questions to configure the harness."

This step is critical — your interview questions should be INFORMED by what
you found in the code, not generic. If you see a PyTorch training loop, ask
about training-specific evaluation. If you see a FastAPI app, ask about
endpoint testing. If you see test files, reference them by name.

### 1. Project Basics
- What are you building? (web app, ML model, data pipeline, API, etc.)
- What's the tech stack? (If you explored the codebase, confirm what you found.)
- Suggest a project name (lowercase, hyphenated).
- After agreement: write config.json with name, model, budget, target_repo.

### 2. Evaluation Dimensions
This is the most important section. The harness uses evidence-based evaluation,
not subjective scoring. For each dimension:

- Ask what "good" looks like CONCRETELY for their domain.
- Push back on vague criteria — "high quality" is not a check. "PR-AUC >= 0.85
  on holdout set" is a check.
- For each dimension, define concrete checks that are either:
  - pass_fail: binary yes/no (e.g., "no data leakage")
  - metric: a measurable number with a concrete threshold (e.g., "test
    coverage >= 80%", "P95 latency < 200ms")
- Ask whether each dimension is "blocking" (fails the task) or "warning".
- Challenge arbitrary numbers: "What does 0.85 mean? Is that based on your
  baseline? Your production bar? Or a guess?"

Example conversation flow:
  You: "For data quality, what concrete checks matter?"
  User: "No leakage, balanced classes, clean labels"
  You: "For class balance — what ratio? Is 20% minority realistic or do you
       have heavily imbalanced data?"
  User: "It's imbalanced, 5:1 ratio is normal"
  You: "OK, so the check is: minority class >= 15% of majority — that's
       slightly above your natural ratio to push for better balance. Sound right?"

After agreement on dimensions: update config.json with evaluation_dimensions.

### 3. Evaluator Capabilities
- Does the evaluator need special tools? (MCP servers, custom scripts)
- For ML: does it need to run model inference? Access a test dataset?
- For web apps: does it need browser automation?
- After agreement: update config.json with evaluator_mcp_servers and tool lists.

### 4. Agent Prompts
Based on everything discussed, generate:
- prompts/planner.md — how to decompose tasks for this domain
- prompts/generator.md — coding/building style for this stack
- prompts/evaluator.md — evaluation strategy referencing the declared dimensions
Write these files to the project directory.

### 5. Review
Show the user the complete config.json and ask for final adjustments.

## Rules
- One section at a time. Don't skip ahead.
- Ask ONE question at a time. Don't overwhelm with multiple questions.
- When filling in config, show the user what you're writing and get approval.
- Push back on vague criteria. Your job is to make evaluation concrete.
- Use the Write tool to create files. IMPORTANT: always use ABSOLUTE paths
  starting with {projects_dir}/<project-name>/. Never use relative paths.
  Example: {projects_dir}/my-project/config.json

## Output
When completely done, output: PROJECT_NAME: <name>

The projects directory is at: {projects_dir}
All project files MUST be written under: {projects_dir}/<project-name>/
"""


async def _find_or_create_project(
    projects_dir: Path,
    raw_name: str,
    description: str,
    target_repo: str | None,
    websocket: WebSocket,
) -> str:
    """Find the project the configurator created, or scaffold one as fallback.

    The agent may have created the project under a slightly different name
    than what it output as PROJECT_NAME. This function checks:
    1. Exact name match
    2. Any new project created during this session (by modified time)
    3. Fallback: scaffold with the raw name
    """
    from agent_harness.config import HarnessConfig

    # 1. Exact match
    exact = projects_dir / raw_name
    if (exact / "config.json").exists():
        return raw_name

    # 2. Scan for any project with config.json (may have been created with
    #    a different name — underscores, different casing, etc.)
    if projects_dir.exists():
        candidates = []
        for entry in projects_dir.iterdir():
            if entry.is_dir() and (entry / "config.json").exists():
                candidates.append(entry)
        # Check if any project was created in the last 60 seconds
        import time
        now = time.time()
        recent = [c for c in candidates if now - c.stat().st_mtime < 60]
        if recent:
            # Pick the most recently modified
            best = max(recent, key=lambda c: c.stat().st_mtime)
            return best.name

    # 3. Fallback: scaffold
    await websocket.send_json({
        "type": "message",
        "text": f"Project files not found. Creating scaffold for '{raw_name}'...",
    })
    project_dir = projects_dir / raw_name
    HarnessConfig.scaffold_project(project_dir, name=raw_name)
    if target_repo:
        config_path = project_dir / "config.json"
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["target_repo"] = target_repo
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return raw_name


def _summarize_tool_use(tool_name: str, tool_input: dict) -> str:
    """Create a human-readable summary of a tool invocation."""
    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"Scanning for files: {pattern}"
    elif tool_name == "Read":
        path = tool_input.get("file_path", "")
        # Show just the filename, not the full path
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Reading: {short}"
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"Searching for: {pattern}"
    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Writing: {short}"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"Running: {cmd[:60]}{'...' if len(cmd) > 60 else ''}"
    else:
        return f"Using {tool_name}"


async def _sdk_configure(
    websocket: WebSocket, description: str, sdk: Any,
    target_repo: str | None = None,
) -> str:
    """Use claude-agent-sdk to run a multi-turn configurator conversation."""
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
    )

    projects_dir = _projects_dir()
    system_prompt = _CONFIGURATOR_SYSTEM_PROMPT.format(
        projects_dir=projects_dir.resolve()
    )

    # Build the initial message with target_repo context if provided
    user_message = f"I want to create an agent harness project for: {description}"
    user_message += f"\n\nWrite all project files to: {projects_dir.resolve()}/<project-name>/"
    if target_repo:
        user_message += f"\n\nThe existing codebase to explore is at: {target_repo}"
        user_message += "\nPlease explore the codebase first (Step 0) before asking questions."

    all_output: list[str] = []

    # If target_repo is provided, set cwd to the repo so the configurator
    # can Read/Glob/Grep the actual codebase. It writes project files to
    # projects_dir via absolute paths.
    agent_cwd = target_repo if target_repo else str(projects_dir.resolve())

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=["Write", "Read", "Bash", "Glob", "Grep"],
        cwd=agent_cwd,
        permission_mode="acceptEdits",
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_message)

        first_turn = True

        # Multi-turn conversation loop
        while True:
            turn_output: list[str] = []

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            turn_output.append(block.text)
                            await websocket.send_json({"type": "message", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            # Forward tool activity so the UI shows exploration progress
                            tool_summary = _summarize_tool_use(block.name, block.input)
                            await websocket.send_json({
                                "type": "tool_activity",
                                "tool": block.name,
                                "summary": tool_summary,
                            })

                elif isinstance(message, ResultMessage):
                    # ResultMessage.result is the aggregated final text — but
                    # it was already streamed via AssistantMessage TextBlocks.
                    # Only use it if nothing was captured from AssistantMessage
                    # (e.g., SDK returned result without streaming).
                    if message.result and not turn_output:
                        turn_output.append(message.result)
                        await websocket.send_json({"type": "message", "text": message.result})

            all_output.extend(turn_output)

            # After the first turn with a target_repo, verify the agent
            # actually explored the codebase before proceeding to questions.
            if first_turn and target_repo:
                first_turn = False
                first_turn_text = "\n".join(turn_output).lower()
                explored = any(
                    signal in first_turn_text
                    for signal in ["i see", "i found", "the codebase", "project structure",
                                   "directory", "files", "explored", "repository"]
                )
                if not explored:
                    # Agent skipped exploration — nudge it
                    await websocket.send_json({
                        "type": "message",
                        "text": "[System: The configurator should explore the codebase before asking questions. Requesting exploration...]",
                    })
                    await client.query(
                        "Before asking me questions, please explore the codebase first. "
                        "Use Glob and Read to understand the project structure, tech stack, "
                        "and existing patterns. Then summarize what you found."
                    )
                    continue
            else:
                first_turn = False

            # Check if the configurator is done
            full_text = "\n".join(all_output)
            if "PROJECT_NAME:" in full_text:
                break

            # Not done — wait for user's reply over WebSocket
            try:
                raw = await websocket.receive_text()
                reply = json.loads(raw)
                user_reply = reply.get("text", reply.get("description", ""))
                if not user_reply:
                    continue
                await client.query(user_reply)
            except WebSocketDisconnect:
                break

    # Extract project name from output
    import re
    full_output = "\n".join(all_output)
    match = re.search(r"PROJECT_NAME:\s*(\S+)", full_output)
    if match:
        return match.group(1)

    project_name = re.sub(r"[^a-z0-9]+", "-", description.lower().strip())[:40].strip("-")
    return project_name or "new-project"
