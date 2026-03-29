# Agent Harness Framework — Design Spec

## Context

Building an agent harness framework inspired by [Anthropic's blog post on harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps). The framework orchestrates three AI agents — Planner, Generator, and Evaluator — in a feedback loop to produce high-quality code autonomously. The key insight: separating generation from evaluation produces dramatically better results than a single agent, because agents are bad at self-critique.

The framework must support per-project customization (e.g., game dev vs. Android dev) without modifying the base harness code, support both greenfield and existing-repo workflows, and provide an AI-assisted web UI for generating project configurations.

## Architecture Overview

```
                    Web UI (Configure + Monitor)
                          |
                    Configurator Agent
                          |
                     config.json + prompts/
                          |
              +-----------+-----------+
              |     Orchestrator      |
              |                       |
              |  Planner ─► Plan      |
              |    (tasks + spec)     |
              |       |               |
              |  ┌─── ▼ ──────────┐   |
              |  │ per task:      │   |
              |  │  Generator ─►  │   |
              |  │  Evaluator ─►  │   |
              |  │  quality ok? ↺ │   |
              |  └────────────────┘   |
              |       |               |
              |  Orchestrator checks: |
              |  budget, progress,    |
              |  evaluator finish ──► done
              +-----------------------+
```

## Two-Layer Design

### Layer 1: Base Harness (the framework)

A pip-installable Python package with two architectural components:

**Runtime** (headless, the core):
- Protocols for `Planner`, `Generator`, `Evaluator`
- Default SDK-backed implementations
- Orchestrator with run-state management
- Artifact system and run-state persistence

**Configurator** (optional, sits on top):
- FastAPI web UI for AI-assisted project setup
- Run monitoring and project management
- Architecturally separate — the runtime works without it

```
agent_harness/
  __init__.py
  # --- Runtime ---
  core.py              # Orchestrator, protocols, run-state machine
  planner.py           # Default Planner implementation
  generator.py         # Default Generator implementation
  evaluator.py         # Default Evaluator implementation
  artifacts.py         # Data models + file I/O
  config.py            # HarnessConfig, project loading
  state.py             # RunState persistence and resumption
  cli.py               # CLI entry point
  # --- Configurator (optional layer) ---
  server.py            # FastAPI web UI + configurator agent
  static/              # Web UI frontend
```

### Layer 2: Project Instances (branched out)

Each customized harness is a self-contained project directory:

```
projects/
  game-dev/
    config.json          # Harness configuration
    prompts/
      planner.md         # System prompt for the planner
      generator.md       # System prompt for the generator
      evaluator.md       # System prompt + grading criteria
    runs/                # One directory per harness run
      2026-03-28_14-30/
        run_state.json   # Machine state: phase, spend, sessions, status (orchestrator writes)
        plan.json        # Machine-readable: tasks, contracts, dependencies (orchestrator writes)
        plan.md          # Human-readable: spec + task list (planner writes, agents read)
        tasks/
          01_setup-auth/
            contract.json    # Machine-readable contract (from plan.json, orchestrator writes)
            contract.md      # Human-readable contract (planner writes, agents read)
            generation.json  # Machine-readable: task_id, files_changed (orchestrator writes after generator)
            generation.md    # Human-readable work summary (generator writes)
            evaluation.json  # Machine-readable: scores, check_results, verdict (evaluator writes)
            evaluation.md    # Human-readable evaluation report (evaluator writes)
          02_add-api/
            ...
        logs/

  android-dev/
    config.json
    prompts/
      ...
    runs/
```

Projects are fully isolated. They share the base harness code but never interfere with each other.

## Run State Model

Each run persists an explicit `run_state.json`:

```python
class RunStatus(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"         # All tasks passed, run succeeded
    HALTED = "halted"               # Evaluator issued HALT_RUN (quality gate or futility)
    FAILED = "failed"               # Unrecoverable error (agent crash, etc.)
    PAUSED = "paused"               # Orchestrator safety stop (budget, no-progress)

@dataclass
class RunState:
    run_id: str
    status: RunStatus
    status_reason: str | None       # Why the run entered its current status
    current_phase: str              # "planning" | "task_N" | "done"
    current_task_index: int
    total_tasks: int
    iterations_on_current_task: int
    plan_version: int               # Incremented on each replan
    completed_task_ids: list[str]   # Stable IDs of tasks completed across replans
    cumulative_spend_usd: float
    session_ids: dict[str, str]     # Keyed by stage+context: "planner:v1", "generator:task-01", "evaluator:task-01:iter-2"
    artifact_manifest: list[str]    # Paths to all written artifacts
    started_at: str
    updated_at: str
```

This enables:
- **Resumability**: Read the last state, continue from where we left off
- **Cost tracking**: Cumulative spend across all agent calls
- **Progress reporting**: The web UI reads this to show status
- **Post-mortem**: Full audit trail of what happened

## Core Data Models

```python
@dataclass
class Plan:
    task_description: str           # Original user request
    spec: str                       # High-level spec / product context
    tasks: list[Task]               # Ordered task list
    acceptance_criteria: list[str]  # Overall success criteria
    raw_text: str

@dataclass
class Contract:
    success_criteria: list[str]     # Concrete, testable conditions for "done"
    scope_boundaries: str           # What's in/out of scope for this task

@dataclass
class Task:
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    dependencies: list[str]         # IDs of tasks that must complete first
    contract: Contract              # Planner-authored definition of "done"

@dataclass
class GenerationResult:
    task_id: str
    summary: str
    files_changed: list[str]
    raw_text: str

class Verdict(str, Enum):
    ADVANCE_TASK = "advance_task"       # Task met its contract, move to next
    RETRY_TASK = "retry_task"           # Task needs rework, feedback provided
    REQUEST_REPLAN = "request_replan"   # Plan is invalid, re-invoke planner
    HALT_RUN = "halt_run"              # Stop the entire run (quality gate or futility)

@dataclass
class EvaluationResult:
    task_id: str
    verdict: Verdict                # Single explicit control flow signal
    scores: dict[str, float]        # Criterion-level scores
    check_results: list[CheckResult]  # Individual check outcomes
    feedback: str                   # Actionable feedback for generator (on retry)
    replan_reason: str | None       # Why replanning is needed (on request_replan)
    raw_text: str

@dataclass
class CheckResult:
    name: str                       # e.g., "pytest", "lint", "ai_review"
    passed: bool
    output: str                     # stdout/stderr or AI review text
    severity: str                   # "blocking" | "warning" | "info"

@dataclass
class UsageInfo:
    input_tokens: int
    output_tokens: int
    spend_usd: float                # Computed from tokens × model pricing

@dataclass
class StageExecution[T]:
    result: T                       # The stage's output (Plan, GenerationResult, EvaluationResult)
    usage: UsageInfo                # Token counts and cost for this call
    session_key: str                # Logical key, e.g. "planner:v1", "generator:task-01", "evaluator:task-01:iter-2"
    session_id: str                 # SDK session ID (opaque, for resumption)
```

## Protocols

```python
class Planner(Protocol):
    async def plan(
        self, task: str, cwd: str,
        replan_context: str | None = None,           # Why replanning was requested
        completed_task_ids: list[str] | None = None,  # Tasks already done (survive replan)
    ) -> StageExecution[Plan]: ...

class Generator(Protocol):
    async def generate(self, task: Task, prior_evaluation: EvaluationResult | None, cwd: str) -> StageExecution[GenerationResult]: ...

class Evaluator(Protocol):
    async def evaluate(self, task: Task, result: GenerationResult, cwd: str) -> StageExecution[EvaluationResult]: ...
```

Key design decisions:
- Generator works on a **Task** (bounded unit with an embedded `Contract`)
- Generator receives the full `EvaluationResult` on retry (not a lossy string summary), so it can use structured check results and scores for targeted repair
- All stages return `StageExecution[T]`, which carries `usage` and `session_id` alongside the result — the orchestrator uses this for spend tracking and session management
- `cwd` parameter supports existing repo workflows

## Contract Authorship

The **planner** produces a `Contract` embedded in each `Task`. The contract includes concrete `success_criteria` (testable conditions) and `scope_boundaries` (what's in/out). This keeps "done" definitions explicit and planner-authored — the evaluator grades against the contract but does not invent criteria at runtime.

If the evaluator finds the contract itself is flawed (criteria are untestable, scope is wrong), it returns `Verdict.REQUEST_REPLAN` with a `replan_reason`.

## Orchestrator Loop

The orchestrator owns the run lifecycle and hard stop conditions. The evaluator owns quality judgment via the `Verdict` enum.

```python
class Orchestrator:
    async def run(self, task_description: str) -> RunState:
        plan_exec = await self.planner.plan(task_description, cwd=self.project_cwd)
        plan = plan_exec.result
        self._track(plan_exec)
        self._adopt_plan(plan)
        self.state.transition(RunStatus.EXECUTING)

        while self.state.current_task_index < len(plan.tasks):
            task = plan.tasks[self.state.current_task_index]

            # Skip tasks already completed in a prior plan version
            if task.id in self.state.completed_task_ids:
                self.state.advance_task()
                continue

            prior_evaluation = None

            while True:
                # Orchestrator safety checks
                if self.state.cumulative_spend_usd >= self.config.max_budget_usd:
                    self.state.transition(RunStatus.PAUSED, reason="budget_exhausted")
                    return self.state

                gen_exec = await self.generator.generate(task, prior_evaluation, cwd=self.project_cwd)
                self._track(gen_exec)

                eval_exec = await self.evaluator.evaluate(task, gen_exec.result, cwd=self.project_cwd)
                self._track(eval_exec)
                evaluation = eval_exec.result

                match evaluation.verdict:
                    case Verdict.ADVANCE_TASK:
                        self._commit_checkpoint(task)  # Orchestrator owns git commits
                        self.state.completed_task_ids.append(task.id)
                        self.state.advance_task()
                        break
                    case Verdict.RETRY_TASK:
                        prior_evaluation = evaluation
                        # No-progress detection: same feedback N times → pause
                    case Verdict.REQUEST_REPLAN:
                        plan_exec = await self.planner.plan(
                            task_description, cwd=self.project_cwd,
                            replan_context=evaluation.replan_reason,
                            completed_task_ids=self.state.completed_task_ids,
                        )
                        plan = plan_exec.result
                        self._track(plan_exec)
                        self.state.plan_version += 1
                        self._adopt_plan(plan)
                        self.state.transition(RunStatus.EXECUTING)
                        break
                    case Verdict.HALT_RUN:
                        self.state.transition(RunStatus.HALTED, reason=evaluation.feedback)
                        return self.state

        self.state.transition(RunStatus.COMPLETED)
        return self.state

    def _track(self, execution: StageExecution) -> None:
        """Update spend and session registry from a stage execution."""
        self.state.cumulative_spend_usd += execution.usage.spend_usd
        self.state.session_ids[execution.session_key] = execution.session_id

    def _adopt_plan(self, plan: Plan) -> None:
        """Refresh run state after a new or replanned plan is adopted."""
        self.state.total_tasks = len(plan.tasks)
        self.state.current_task_index = 0
        self.state.current_phase = "task_0"
        self.state.iterations_on_current_task = 0
```

**Termination hierarchy:**
1. **Evaluator** `HALT_RUN` → stop entirely, status = `halted` (quality authority)
2. **Evaluator** `ADVANCE_TASK` → orchestrator commits checkpoint, move to next task
3. **Evaluator** `REQUEST_REPLAN` → re-invoke planner with completed_task_ids as context, increment plan_version
4. **Orchestrator** budget exhaustion → pause
5. **Orchestrator** no-progress detection (same feedback N times) → pause

## Default Agent Implementations

Each wraps `claude_agent_sdk.query()` with long-lived sessions (using SDK session resumption):

### Planner
- **Tools**: `Read`, `Glob`, `Grep` (read-only, to understand existing code)
- **System prompt**: Expand a brief task into a plan with ordered tasks, acceptance criteria, and technical notes
- **Output**: `plan.md` with task list

### Generator
- **Tools**: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`
- **System prompt**: Implement the current task within scope boundaries defined by the contract. Modify the working tree only — do not commit to git (the orchestrator owns commits after evaluation passes).
- **Session**: Long-lived, resumed across iterations on the same task. Fresh session per new task.
- **Working directory**: The actual project/repo directory

### Evaluator (composable pipeline)
The default evaluator runs checks in sequence:

1. **Deterministic checks**: Run tests (`pytest`/etc.), linter, type checker via Bash
2. **AI review**: Claude reviews code quality, spec adherence, security
3. **Scoring**: Aggregate check results into per-criterion scores
4. **Verdict**: Emit `Verdict.ADVANCE_TASK` when all blocking checks pass and scores meet thresholds. Emit `Verdict.RETRY_TASK` with actionable feedback when they don't. Emit `Verdict.REQUEST_REPLAN` if the contract itself is flawed. Emit `Verdict.HALT_RUN` if quality is irrecoverable or further iteration is futile.

This internal pipeline is an implementation detail — custom evaluators can use any approach.

## Existing Repo Support

When `config.json` specifies a `target_repo` path:
- The harness operates inside that directory (no `workspace/project/` needed)
- The planner reads existing code structure before generating the plan
- The generator works directly in the repo
- Git branch isolation: each run creates a working branch (`harness/<run-id>`)
- Artifacts (plans, evaluations, state) still live in the project's `runs/` directory

For greenfield: omit `target_repo` and the harness creates a fresh directory.

## AI-Assisted Configuration (Web UI)

**FastAPI server** (`server.py`) — architecturally separate from the runtime:

1. User opens `localhost:8000`, selects "New Project"
2. Chat interface with a **Configurator Agent** that interviews about tech stack, quality priorities, evaluation criteria
3. Configurator generates the project directory with tailored config and prompts
4. User reviews/edits config in the UI
5. User launches runs and monitors progress (reads `run_state.json`)

The runtime works independently via CLI — the web UI is a convenience layer.

## Customization Levels

1. **AI-assisted** (web UI) — Describe your project, Claude generates the config
2. **Manual config** — Edit `config.json` and prompt `.md` files directly
3. **Component replacement** — Implement the protocols with custom Python classes

## Configuration Schema

```json
{
  "name": "game-dev",
  "model": "claude-opus-4-6",
  "max_budget_usd": 50.0,
  "generator_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
  "evaluator_tools": ["Read", "Bash", "Glob", "Grep"],
  "planner_tools": ["Read", "Glob", "Grep"],
  "target_repo": null,
  "session_mode": "long_lived"
}
```

Prompts live as separate `.md` files in `prompts/`.

## Artifact Format Convention

- **JSON** is the canonical machine-readable format for structured state: `run_state.json`, `plan.json` (tasks, contracts, dependencies), `evaluation.json` (scores, check results, verdict)
- **Markdown** is the human-readable format for agent consumption: `plan.md`, `evaluation.md`, `generation.md`
- Dual format (JSON + markdown) is required for all artifacts with structured data: **plan**, **contract**, **generation**, **evaluation**. The JSON form is the canonical machine-readable representation; the markdown form is what agents read.

## Spend Collection

Each `claude_agent_sdk.query()` call yields `AssistantMessage` objects with a `usage` dict containing `input_tokens` and `output_tokens`. The orchestrator sums these per-call and converts to USD using model-specific pricing constants (a simple lookup table, not a pricing subsystem). `RunState.cumulative_spend_usd` is updated after every agent call.

## Error Handling & Safety

- **Budget cap**: Orchestrator tracks cumulative spend, pauses when exceeded
- **No-progress detection**: Orchestrator pauses if the same feedback recurs across N iterations
- **Agent failures**: `CLIConnectionError`/`ProcessError` caught, logged, run state set to `failed`
- **Resumability**: `run_state.json` + session IDs enable pickup from last completed step
- **Git safety**: Runs on existing repos create isolated branches. The orchestrator owns all git commits — it creates checkpoints after `ADVANCE_TASK`. The generator modifies the working tree only, so rejected work can be discarded cleanly.

## Verification Plan

1. **Unit tests** — Artifact serialization, config loading, run state transitions, project creation
2. **Integration test** — Run harness on trivial task with low budget, verify full loop (plan → task → evaluate → complete)
3. **Existing repo test** — Point harness at a test repo, verify branch creation and code changes
4. **Web UI test** — Launch server, create project via configurator, verify directory structure
5. **Isolation test** — Two projects running, no workspace cross-contamination
6. **Resume test** — Kill a run mid-iteration, resume, verify it continues correctly
