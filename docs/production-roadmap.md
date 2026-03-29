# Agent Harness — Production Roadmap

## Current Status

**Prototype: green.** The core loop works end-to-end (plan → generate → evaluate → advance). 114 unit tests pass. A live smoke test completed successfully (2 tasks, $0.025 spend). Resume path is implemented and tested.

**Production: not yet.** Parser fragility, missing observability, no safety enforcement, narrow test coverage.

---

## P0 — Core Reliability

**Exit criteria:** Planner and evaluator run with schema-validated structured output only. Parse-fallback path is no longer on the critical path. No-progress guardrails prevent infinite retry loops. Regression tests cover all captured real model output patterns.

### 1. Structured Output Migration

**What:** Planner and evaluator become typed RPC-style calls using `ClaudeAgentOptions.output_format` with JSON schema validation. The agent SDK enforces the output schema — the harness receives validated structured data, not free text requiring regex parsing.

**Rollout strategy:** Direct cutover. Production deployments require `output_format` support — this is a hard requirement, not optional. The current regex parser is retained as a legacy compatibility mode for development/testing against older SDK versions. It is disabled by default in production profiles (`"structured_output": true` in config, default `true`). When the legacy path is used, it logs a warning on every invocation to make it visible.

**Design changes:**
- Agent output is the structured JSON payload, not raw text
- Artifacts store both the structured result (`evaluation.json`) and the raw agent transcript (`evaluation_transcript.md`) for debugging
- Parse failures become explicit `StageExecution` failures with `RunStatus.FAILED` and a clear `status_reason`, not silent fallbacks to `Verdict.RETRY_TASK`

### 2. Parser Regression Corpus

**What:** Capture raw text from live runs as test fixtures. Add regression tests that feed real model output through `_parse_plan()` and `_parse_evaluation()` and assert correct extraction.

**Source:** The `evaluation_iter*.md` and `plan.md` artifacts from the live smoke run and the failed infinite-retry run provide the initial corpus.

### 3. No-Progress / Retry-Budget Guardrails

**What:** Two complementary controls:

1. **Retry cap:** Add `max_retries_per_task` to `HarnessConfig` (default: 5). If `state.iterations_on_current_task >= config.max_retries_per_task`, pause with reason `"max_retries_exceeded"`. This is the hard ceiling.

2. **No-progress detection:** Compute a failure fingerprint from each `EvaluationResult`: `(verdict, sorted(failed_check_names), hash(normalized_feedback))`. Track the last N fingerprints. If the same fingerprint repeats `no_progress_threshold` times (default: 3), pause with reason `"no_progress_detected"` — even if the retry cap hasn't been reached. This catches the case where the generator makes the same mistake repeatedly.

Both controls are checked after each `RETRY_TASK` verdict, before the next generate call.

---

## P1 — Operational Hardening

**Exit criteria:** Resume, timeout, and checkpoint-failure scenarios pass end-to-end integration tests. `events.jsonl` captures a complete stage timeline with per-stage latency, spend, and verdict transitions. Safety enforcement tests prove that stages cannot use disallowed tools or escape the workspace boundary.

### 4. Structured Run Logging

**What:** Append-only structured event log at `runs/<id>/logs/events.jsonl`. Each event is a JSON line with:
- `timestamp`, `event_type` (stage_start, stage_end, verdict, checkpoint, error, resume)
- `stage` (planner, generator, evaluator)
- `task_id`, `iteration`
- `latency_ms`, `input_tokens`, `output_tokens`, `spend_usd`
- `verdict` (for evaluation events)
- `error` (for failure events)

The orchestrator emits events after every `_track()` call, verdict match, and checkpoint.

### 5. Session/Iteration ID Normalization

**What:** Replace the evaluator's internal `_eval_count` with `state.iterations_on_current_task` for session key generation. Align artifact filenames with session keys so `evaluator:task-01:iter-2` in `run_state.json` corresponds to `evaluation_iter2.json` in the task directory.

### 6. Safety & Isolation Enforcement

**What:** Runtime enforcement, not just configuration:

- **Stage-scoped capability enforcement:** The orchestrator validates that each stage's `allowed_tools` list is passed to the SDK. The evaluator cannot use `Write` or `Edit`. The planner cannot use `Bash`. This is already configured but not verified at runtime.
- **Workspace root enforcement:** Before each stage call, the orchestrator resolves `cwd` to its canonical path (`Path.resolve()`) and asserts it is within the expected project boundary. This guards against symlink escapes and relative-path traversal. Additionally, a `PostToolUse` hook on file-writing tools (`Write`, `Edit`) validates that the target path's canonical form is within the workspace root — this catches absolute-path writes and symlinked paths at the point of execution, not just at stage entry.
- **Policy tests:** Integration tests that prove:
  - An evaluator configured with `["Read", "Bash", "Glob", "Grep"]` cannot write files
  - A planner configured with `["Read", "Glob", "Grep"]` cannot execute shell commands
  - No stage can operate outside the project `cwd` (tested with absolute paths, `../` traversal, and symlinked paths)
- **Per-stage timeout:** Add `stage_timeout_seconds` to `HarnessConfig` (default: 300 for planner/evaluator, 600 for generator). The orchestrator wraps each `query()` call in `anyio.fail_after()`.
- **Failure classification:** Categorize errors as:
  - `retriable` — SDK transient errors (`CLIConnectionError`, rate limits) → automatic retry with backoff
  - `terminal` — agent crash, schema validation failure → `RunStatus.FAILED`
  - `policy` — budget exhaustion, max retries, timeout → `RunStatus.PAUSED`

### 7. Resume Hardening

**What:** Real interrupted-run integration tests beyond mocked agents:
- Resume after SDK timeout mid-generation
- Resume after partial artifact write (only `.json` written, `.md` missing)
- Resume after git checkpoint failure
- Resume with accumulated spend near budget limit

---

## P2 — Existing-Repo Workflows

**Exit criteria:** Existing-repo runs handle dirty worktree detection, baseline failing tests, and branch isolation without corrupting the source repository. Validated against a matrix of at least 3 repos: (a) a clean Python repo, (b) a repo with pre-existing failing tests, and (c) a repo with a dirty worktree at start. All three complete without corrupting the source repo or losing uncommitted work.

### 8. Branch Hygiene

**What:**
- Dirty worktree detection before run start — warn or abort if uncommitted changes exist
- Clean branch creation: `harness/<run-id>` branch from current HEAD
- Branch teardown option after successful run (configurable: keep or delete)
- Conflict detection: if the base branch moves during a long run, detect and warn

### 9. Repo-Aware Evaluation

**What:**
- Auto-detect test commands: look for `pytest`, `npm test`, `make test`, `cargo test` in the repo
- Baseline test results: run tests before any generation to establish pre-existing failures
- Evaluator only fails on *new* test failures, not pre-existing ones
- Configure repo-specific test/lint commands in `config.json`

### 10. Stress Scenarios

**What:** End-to-end tests covering:
- Multi-task project with inter-task dependencies
- Replan flow triggered by evaluator
- Budget exhaustion mid-task → pause → resume → complete
- Malformed model output (missing sections, wrong format)
- Partial tool failure (Bash command fails, file not found)
- Large project with 10+ tasks

---

## P3 — Operator Tooling

**Exit criteria:** An operator can inspect a run's full history, diagnose failures, and understand cost breakdown without reading raw JSON files.

### 11. Run Inspection CLI

**What:** `python -m agent_harness inspect <project> <run-id>` shows:
- Stage-by-stage event timeline with latency and spend
- Verdict history per task
- Artifact manifest with links
- Error log summary

### 12. Failure Triage UI

**What:** Web UI enhancements:
- Event timeline visualization
- Per-stage spend breakdown chart
- Side-by-side diff of generation iterations
- Links to artifact files

### 13. Cancellation Semantics

**What:** Graceful stop mid-stage:
- `python -m agent_harness cancel <project> <run-id>`
- Sends interrupt to the running agent session
- Saves state as `PAUSED` with reason `"user_cancelled"`
- Preserves all artifacts written so far
