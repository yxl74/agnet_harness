# Agent Harness

A customizable framework for orchestrating AI agents in a **Planner / Generator / Evaluator** feedback loop, inspired by [Anthropic's harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps).

The harness separates *generation* from *evaluation* — because agents are bad at self-critique. An independent evaluator grades every task against a contract, and the orchestrator controls retries, replanning, budget, and git checkpoints.

## Quick Start

```bash
# Install
pip install -e .
# or
uv pip install -e .

# Set your API key
export ANTHROPIC_API_KEY="sk-..."

# Create a project
python -m agent_harness new my-project

# Run the harness
python -m agent_harness run my-project "Build a calculator CLI in Python with tests"

# Launch the web UI
python -m agent_harness serve
# Open http://localhost:8000
```

## How It Works

```
User: "Build a todo app"
         |
    Planner ──► Plan (ordered tasks with contracts)
         |
    ┌────▼────────────┐
    │  per task:       │
    │   Generator ──►  │  (writes code)
    │   Evaluator ──►  │  (runs tests, reviews, scores)
    │   verdict?   ↺   │
    └──────────────────┘
         |
    Orchestrator: commit checkpoint, next task
         |
    Done (or paused/halted)
```

1. **Planner** expands a brief task into an ordered list of sub-tasks, each with a *contract* defining "done"
2. **Generator** implements one task at a time, reading the contract and any prior feedback
3. **Evaluator** runs tests, lints, reviews code, scores on multiple dimensions, and emits a verdict:
   - `ADVANCE_TASK` — task passed, move to next
   - `RETRY_TASK` — needs rework, feedback provided
   - `REQUEST_REPLAN` — the plan itself is wrong, re-invoke planner
   - `HALT_RUN` — stop entirely (quality too low or further iteration futile)
4. **Orchestrator** owns the lifecycle: budget checks, no-progress detection, git commits, state persistence

## Project Structure

```
agent_harness/
  core.py          # Orchestrator, Protocol interfaces
  planner.py       # Default planner (Claude Agent SDK)
  generator.py     # Default generator (Claude Agent SDK)
  evaluator.py     # Default evaluator (composable pipeline)
  artifacts.py     # Data models (Plan, Task, Contract, EvaluationResult, ...)
  state.py         # RunState persistence, RunStatus enum
  config.py        # HarnessConfig, project scaffolding
  cli.py           # CLI entry point (run, resume, new, list, serve)
  server.py        # FastAPI web UI + AI configurator
  static/          # Web UI frontend
  templates/       # Default project template
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `python -m agent_harness new <project>` | Scaffold a new project with default config and prompts |
| `python -m agent_harness run <project> "task"` | Run the harness loop |
| `python -m agent_harness resume <project> <run-id>` | Resume a paused or failed run |
| `python -m agent_harness list` | List projects and their runs |
| `python -m agent_harness serve` | Launch the web UI at http://localhost:8000 |

## Project Layout

Each project is a self-contained directory:

```
projects/my-project/
  config.json        # Model, budget, tools, retry limits
  prompts/
    planner.md       # System prompt for the planner
    generator.md     # System prompt for the generator
    evaluator.md     # System prompt + grading criteria
  runs/              # One directory per harness run
    20260328_143000/
      run_state.json     # Status, spend, progress, session IDs
      plan.json / .md    # Planner output
      tasks/
        0_task-01/
          contract.json / .md
          generation_iter0.json / .md
          evaluation_iter0.json / .md
      project/           # Generated code (greenfield)
      logs/              # Git errors, etc.
```

## Configuration

Edit `config.json` in your project directory:

```json
{
  "name": "my-project",
  "model": "claude-sonnet-4-6",
  "max_budget_usd": 5.0,
  "max_retries_per_task": 5,
  "no_progress_threshold": 3,
  "structured_output": true,
  "target_repo": null,
  "generator_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "evaluator_tools": ["Read", "Bash", "Glob", "Grep"],
  "planner_tools": ["Read", "Glob", "Grep"]
}
```

| Field | Description |
|-------|-------------|
| `model` | Claude model ID (`claude-opus-4-6`, `claude-sonnet-4-6`) |
| `max_budget_usd` | Safety cap on total spend per run |
| `max_retries_per_task` | Hard ceiling on generate/evaluate cycles per task |
| `no_progress_threshold` | Pause if the same failure fingerprint repeats this many consecutive times |
| `structured_output` | Use schema-validated JSON output (recommended; `true` by default) |
| `target_repo` | Path to an existing repo (or `null` for greenfield generation) |

## Customization

### Level 1: Prompts

Edit the `.md` files in `prompts/` to customize agent behavior for your domain. The planner prompt controls task decomposition, the generator prompt controls coding style, and the evaluator prompt controls grading criteria.

### Level 2: Config

Adjust model, tools, budget, and retry limits in `config.json`. Different projects can use different models and tool sets.

### Level 3: Component Replacement

Implement the `Planner`, `Generator`, or `Evaluator` protocol with custom Python classes:

```python
from agent_harness.core import Orchestrator
from agent_harness.artifacts import (
    Task, GenerationResult, EvaluationResult,
    StageExecution, UsageInfo, Verdict, CheckResult,
)

class MyEvaluator:
    async def evaluate(self, task: Task, result: GenerationResult, cwd: str) -> StageExecution[EvaluationResult]:
        # Run your own test suite, custom checks, etc.
        ...

orchestrator = Orchestrator(
    planner=DefaultPlanner(...),
    generator=DefaultGenerator(...),
    evaluator=MyEvaluator(),
    config=config,
    run_dir=run_dir,
)
await orchestrator.run("Build something")
```

## Existing Repo Support

Set `target_repo` in config to work inside an existing codebase:

```json
{
  "target_repo": "/path/to/your/repo"
}
```

The harness creates a `harness/<run-id>` branch, operates inside the repo, and commits checkpoints after each task passes evaluation.

## Web UI

Launch with `python -m agent_harness serve` and open http://localhost:8000.

Features:
- **Project list** — view all projects and run counts
- **Run management** — start runs, view run history with status badges
- **Live monitoring** — watch status, spend, task progress in real time (auto-refreshes)
- **AI configurator** — describe a project in natural language, Claude generates the config and prompts

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/projects` | List all projects |
| `GET` | `/api/projects/{name}/runs` | List runs for a project |
| `GET` | `/api/projects/{name}/runs/{id}/state` | Get run state |
| `POST` | `/api/projects/{name}/run` | Start a new run (`{"task": "..."}`) |
| `WS` | `/ws/configure` | AI configurator chat |

## Safety & Guardrails

- **Budget cap** — orchestrator pauses when `cumulative_spend_usd >= max_budget_usd`
- **Retry cap** — pauses after `max_retries_per_task` attempts on a single task
- **No-progress detection** — pauses if the same failure fingerprint repeats `no_progress_threshold` consecutive times
- **Stage error handling** — exceptions from agents are caught and persisted as `RunStatus.FAILED` with descriptive reason
- **Git safety** — orchestrator owns all commits (generator only modifies working tree); runs on existing repos use isolated branches
- **Resumability** — all state persisted to `run_state.json` after every mutation; paused/failed runs can be resumed

## Architecture

The harness has two layers:

**Runtime** (headless core):
- Protocol interfaces (`Planner`, `Generator`, `Evaluator`)
- Orchestrator with state machine, budget enforcement, git checkpoints
- Default SDK-backed agent implementations
- Artifact system with dual JSON + markdown persistence

**Configurator** (optional web layer):
- FastAPI server with REST API
- WebSocket-based AI project configurator
- Run monitoring dashboard

See [docs/superpowers/specs/2026-03-28-agent-harness-design.md](docs/superpowers/specs/2026-03-28-agent-harness-design.md) for the full design spec and [docs/production-roadmap.md](docs/production-roadmap.md) for the production hardening roadmap.

## Requirements

- Python >= 3.12
- `claude-agent-sdk`
- `fastapi`, `uvicorn` (for web UI)
- `ANTHROPIC_API_KEY` environment variable

## License

MIT
