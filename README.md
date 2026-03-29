# Agent Harness

A customizable framework for orchestrating AI agents in a **Planner / Generator / Evaluator** feedback loop, inspired by [Anthropic's harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps).

The harness separates *generation* from *evaluation* — because agents are bad at self-critique. An independent evaluator grades every task against concrete, evidence-based checks with hard thresholds, and the orchestrator controls retries, replanning, budget, and git checkpoints.

## Quick Start

```bash
# Install
pip install -e .
# or
uv pip install -e .

# Create a project
python -m agent_harness new my-project

# Run the harness
python -m agent_harness run my-project "Build a calculator CLI in Python with tests"

# Launch the web UI
python -m agent_harness serve
# Open http://localhost:8000
```

Authentication is handled by the Claude Agent SDK, which uses your existing Claude subscription via OAuth. No API key export required.

## How It Works

```
User: "Build a todo app"
         |
    Planner ──► Plan (ordered tasks with contracts)
         |
    ┌────▼──────────────────────────────┐
    │  per task:                        │
    │   Generator ──► (self-evaluates)  │
    │   Evaluator ──► (runs checks)    │
    │   Orchestrator: threshold check   │
    │   verdict?   ↺                    │
    └───────────────────────────────────┘
         |
    Orchestrator: commit checkpoint, next task
         |
    Done (or paused/halted)
```

1. **Planner** explores the codebase first, then expands a brief task into an ordered list of sub-tasks, each with a *contract* defining "done"
2. **Generator** implements one task at a time, self-evaluates (runs tests, checks contract) before handing off
3. **Evaluator** runs concrete checks (pass/fail and metric-with-threshold), reviews code, and emits a verdict:
   - `ADVANCE_TASK` — task passed, move to next
   - `RETRY_TASK` — needs rework, structured feedback provided
   - `REQUEST_REPLAN` — the plan itself is wrong, re-invoke planner
   - `HALT_RUN` — stop entirely (quality too low or further iteration futile)
4. **Orchestrator** enforces hard thresholds on evaluator scores (overrides lenient evaluations), manages budget, detects no-progress loops, owns git commits, and persists state for resumability

## Key Features

- **Evidence-based evaluation** — Evaluation dimensions have concrete checks (pass/fail or metric-with-threshold), not subjective AI scores. The orchestrator enforces thresholds, not the evaluator AI.
- **Customizable per project** — Each project defines its own evaluation dimensions, tools, prompts, and MCP servers. An ML project evaluates data quality and model accuracy; a web app evaluates UI behavior and API correctness.
- **AI-assisted configuration** — A multi-turn web configurator interviews you about your domain, explores your existing codebase, and generates tailored config with concrete evaluation checks.
- **Pipeline visualization** — Review the full Planner → Generator → Evaluator workflow visually — stages, tools, evaluation dimensions, thresholds — before starting a run.
- **Existing repo support** — Point the harness at an existing codebase. The planner reads the code first; the orchestrator creates isolated git branches and commits checkpoints.
- **Resumable runs** — All state persisted after every mutation. Paused or failed runs resume from exactly where they stopped, with prior evaluation feedback restored.

## Project Structure

```
agent_harness/
  core.py          # Orchestrator, Protocol interfaces
  planner.py       # Default planner (Claude Agent SDK)
  generator.py     # Default generator (Claude Agent SDK)
  evaluator.py     # Default evaluator (composable pipeline)
  artifacts.py     # Data models (Plan, Task, Contract, EvaluationDimension, ...)
  state.py         # RunState persistence, RunStatus enum
  config.py        # HarnessConfig, project scaffolding
  cli.py           # CLI entry point (run, resume, new, list, serve)
  server.py        # FastAPI web UI + AI configurator
  static/          # Web UI frontend (pipeline visualization)
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
  config.json        # Model, budget, tools, evaluation dimensions
  prompts/
    planner.md       # System prompt for the planner
    generator.md     # System prompt for the generator
    evaluator.md     # System prompt + grading criteria
  runs/              # One directory per harness run
    20260328_143000/
      run_state.json          # Status, spend, progress, session IDs
      evaluation_progress.json # Score trends across iterations
      plan.json / .md         # Planner output
      tasks/
        0_task-01/
          contract.json / .md
          generation_iter0.json / .md
          evaluation_iter0.json / .md
      project/                # Generated code (greenfield)
      logs/                   # Git errors, etc.
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
  "planner_tools": ["Read", "Glob", "Grep"],
  "evaluation_dimensions": [
    {
      "name": "data_quality",
      "description": "Data integrity and suitability for training",
      "severity": "blocking",
      "checks": [
        {"name": "no_data_leakage", "check_type": "pass_fail", "description": "No target leakage in features"},
        {"name": "missing_values_pct", "check_type": "metric", "threshold": 0.05, "description": "< 5% missing values"}
      ]
    }
  ],
  "evaluator_mcp_servers": {
    "model-runner": {"command": "python", "args": ["tools/run_model.py"]}
  }
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
| `evaluation_dimensions` | Project-specific evaluation with concrete checks and thresholds |
| `evaluator_mcp_servers` | MCP servers for domain-specific evaluation tools |

### Evaluation Dimensions

Each dimension has concrete checks — not subjective 0-1 scores:

- **pass_fail** checks: binary yes/no (e.g., "no data leakage"). The evaluator reports evidence; the harness decides pass/fail.
- **metric** checks: the evaluator measures a value; the harness enforces the threshold (e.g., "PR-AUC >= 0.85").

Dimensions with `"severity": "blocking"` cause task failure if any check fails. `"warning"` dimensions are logged but don't block.

## Customization

### Level 1: AI-Assisted Configuration (Web UI)

The web configurator interviews you about your domain and generates the full config:

1. Open http://localhost:8000 → Configure
2. Optionally provide a path to your existing codebase
3. The configurator explores the codebase, then asks informed questions
4. It progressively fills in config.json with evaluation dimensions, thresholds, and prompts
5. Review the pipeline visualization before launching

### Level 2: Prompts & Config

Edit the `.md` files in `prompts/` and `config.json` directly. Different projects can use different models, tool sets, evaluation dimensions, and MCP servers.

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

The planner reads the codebase first (Glob structure, Read key files, Grep patterns) before planning. The harness creates a `harness/<run-id>` branch, operates inside the repo, and commits checkpoints after each task passes evaluation.

## Web UI

Launch with `python -m agent_harness serve` and open http://localhost:8000.

### Features

- **Project list** — view all projects with run counts
- **Pipeline visualization** — review the full Planner → Generator → Evaluator workflow before launching: stages with tools, evaluation dimensions with checks and thresholds, config summary
- **AI configurator** — multi-turn interview that explores your codebase, asks domain-specific questions, and progressively builds config with concrete evaluation checks
- **Run management** — start runs, view run history with status badges
- **Live monitoring** — watch status, spend, task progress in real time (auto-refreshes)

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/projects` | List all projects |
| `GET` | `/api/projects/{name}/config` | Get project config + prompts |
| `GET` | `/api/projects/{name}/runs` | List runs for a project |
| `GET` | `/api/projects/{name}/runs/{id}/state` | Get run state |
| `POST` | `/api/projects/{name}/run` | Start a new run (`{"task": "..."}`) |
| `WS` | `/ws/configure` | AI configurator chat (`{"type":"configure", "description":"...", "target_repo":"..."}`) |

## Safety & Guardrails

- **Threshold enforcement** — orchestrator enforces hard thresholds on evaluation checks, overriding lenient evaluator verdicts
- **Budget cap** — orchestrator pauses when `cumulative_spend_usd >= max_budget_usd`
- **Retry cap** — pauses after `max_retries_per_task` attempts on a single task
- **No-progress detection** — pauses if the same failure fingerprint (verdict + failed checks + feedback hash) repeats consecutively
- **Stage error handling** — exceptions from agents are caught and persisted as `RunStatus.FAILED` with descriptive reason
- **Git safety** — orchestrator owns all commits (generator only modifies working tree); runs on existing repos use isolated branches
- **Resumability** — all state persisted to `run_state.json` after every mutation; paused/failed runs resume from exactly where they stopped with prior evaluation feedback restored

## Architecture

The harness has two layers:

**Runtime** (headless core):
- Protocol interfaces (`Planner`, `Generator`, `Evaluator`)
- Orchestrator with state machine, threshold enforcement, budget control, git checkpoints
- Default SDK-backed agent implementations with structured output
- Artifact system with dual JSON + markdown persistence
- Evaluation progress tracking across iterations

**Configurator** (optional web layer):
- FastAPI server with REST API
- Multi-turn WebSocket configurator that explores codebases and generates config
- Pipeline visualization for pre-run workflow review
- Run monitoring dashboard

See [docs/superpowers/specs/2026-03-28-agent-harness-design.md](docs/superpowers/specs/2026-03-28-agent-harness-design.md) for the full design spec and [docs/production-roadmap.md](docs/production-roadmap.md) for the production hardening roadmap.

## Requirements

- Python >= 3.12
- `claude-agent-sdk` (uses your Claude subscription via OAuth — no API key export needed)
- `fastapi`, `uvicorn` (for web UI)

## License

MIT
