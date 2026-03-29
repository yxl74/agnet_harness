"""Run state persistence and transition management for the agent harness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class RunStatus(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"  # All tasks passed, run succeeded
    HALTED = "halted"        # Evaluator issued HALT_RUN (quality gate or futility)
    FAILED = "failed"        # Unrecoverable error (agent crash, etc.)
    PAUSED = "paused"        # Orchestrator safety stop (budget, no-progress)


def _utcnow() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunState:
    run_id: str
    status: RunStatus
    status_reason: str | None        # Why the run entered its current status
    current_phase: str               # "planning" | "task_N" | "done"
    current_task_index: int
    total_tasks: int
    iterations_on_current_task: int
    plan_version: int                # Incremented on each replan
    completed_task_ids: list[str]    # Stable IDs of tasks completed across replans
    cumulative_spend_usd: float
    session_ids: dict[str, str]      # Keyed by stage+context: "planner:v1", "generator:task-01", "evaluator:task-01:iter-2"
    artifact_manifest: list[str]     # Paths to all written artifacts
    started_at: str
    updated_at: str

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def new(cls, run_id: str) -> RunState:
        """Create a fresh RunState in PLANNING status."""
        now = _utcnow()
        return cls(
            run_id=run_id,
            status=RunStatus.PLANNING,
            status_reason=None,
            current_phase="planning",
            current_task_index=0,
            total_tasks=0,
            iterations_on_current_task=0,
            plan_version=0,
            completed_task_ids=[],
            cumulative_spend_usd=0.0,
            session_ids={},
            artifact_manifest=[],
            started_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Serialize this RunState to a JSON file, creating parent dirs as needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # RunStatus is a str-Enum, so asdict gives back the string value already;
        # restore it explicitly so the dict round-trips cleanly.
        data["status"] = self.status.value
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> RunState:
        """Deserialize a RunState from a JSON file."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            run_id=data["run_id"],
            status=RunStatus(data["status"]),
            status_reason=data.get("status_reason"),
            current_phase=data.get("current_phase", "planning"),
            current_task_index=data.get("current_task_index", 0),
            total_tasks=data.get("total_tasks", 0),
            iterations_on_current_task=data.get("iterations_on_current_task", 0),
            plan_version=data.get("plan_version", 0),
            completed_task_ids=data.get("completed_task_ids", []),
            cumulative_spend_usd=data.get("cumulative_spend_usd", 0.0),
            session_ids=data.get("session_ids", {}),
            artifact_manifest=data.get("artifact_manifest", []),
            started_at=data["started_at"],
            updated_at=data["updated_at"],
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(self, status: RunStatus, reason: str | None = None) -> None:
        """Update status, status_reason, and updated_at atomically."""
        self.status = status
        self.status_reason = reason
        self.updated_at = _utcnow()

    def advance_task(self) -> None:
        """Move to the next task: increment index, reset iteration counter, update phase."""
        self.current_task_index += 1
        self.iterations_on_current_task = 0
        if self.current_task_index >= self.total_tasks:
            self.current_phase = "done"
        else:
            self.current_phase = f"task_{self.current_task_index}"
        self.updated_at = _utcnow()

    def record_attempt(self) -> None:
        """Record a generate/evaluate attempt on the current task."""
        self.iterations_on_current_task += 1
        self.updated_at = _utcnow()
