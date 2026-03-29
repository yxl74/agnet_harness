from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Generic, Self, TypeVar
import json

T = TypeVar("T")


@dataclass
class Contract:
    success_criteria: list[str]     # Concrete, testable conditions for "done"
    scope_boundaries: str           # What's in/out of scope for this task

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            success_criteria=data["success_criteria"],
            scope_boundaries=data["scope_boundaries"],
        )

    def to_markdown(self) -> str:
        criteria_lines = "\n".join(f"- {c}" for c in self.success_criteria)
        return (
            "## Contract\n\n"
            "### Success Criteria\n\n"
            f"{criteria_lines}\n\n"
            "### Scope Boundaries\n\n"
            f"{self.scope_boundaries}\n"
        )


@dataclass
class Task:
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    dependencies: list[str]         # IDs of tasks that must complete first
    contract: Contract              # Planner-authored definition of "done"

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            id=data["id"],
            title=data["title"],
            description=data["description"],
            acceptance_criteria=data["acceptance_criteria"],
            dependencies=data["dependencies"],
            contract=Contract.from_json(data["contract"]),
        )

    def to_markdown(self) -> str:
        criteria_lines = "\n".join(f"- {c}" for c in self.acceptance_criteria)
        deps = ", ".join(self.dependencies) if self.dependencies else "none"
        return (
            f"## Task: {self.title} (`{self.id}`)\n\n"
            f"{self.description}\n\n"
            f"**Dependencies:** {deps}\n\n"
            "### Acceptance Criteria\n\n"
            f"{criteria_lines}\n\n"
            f"{self.contract.to_markdown()}"
        )


@dataclass
class Plan:
    task_description: str           # Original user request
    spec: str                       # High-level spec / product context
    tasks: list[Task]               # Ordered task list
    acceptance_criteria: list[str]  # Overall success criteria
    raw_text: str

    def to_json(self) -> dict:
        return {
            "task_description": self.task_description,
            "spec": self.spec,
            "tasks": [t.to_json() for t in self.tasks],
            "acceptance_criteria": self.acceptance_criteria,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            task_description=data["task_description"],
            spec=data["spec"],
            tasks=[Task.from_json(t) for t in data["tasks"]],
            acceptance_criteria=data["acceptance_criteria"],
            raw_text=data["raw_text"],
        )

    def to_markdown(self) -> str:
        lines = [
            "# Plan\n",
            "## Request\n",
            f"{self.task_description}\n",
            "## Spec\n",
            f"{self.spec}\n",
            "## Tasks\n",
        ]
        for i, task in enumerate(self.tasks, 1):
            lines.append(f"### {i}. {task.title} (`{task.id}`)\n")
            lines.append(f"{task.description}\n")
            if task.dependencies:
                deps = ", ".join(task.dependencies)
                lines.append(f"**Dependencies:** {deps}\n")
            lines.append("**Acceptance Criteria:**\n")
            for criterion in task.acceptance_criteria:
                lines.append(f"- {criterion}")
            lines.append("")
            lines.append("**Contract — Success Criteria:**")
            for criterion in task.contract.success_criteria:
                lines.append(f"- {criterion}")
            lines.append(f"**Contract — Scope:** {task.contract.scope_boundaries}\n")

        lines.append("## Overall Acceptance Criteria\n")
        for criterion in self.acceptance_criteria:
            lines.append(f"- {criterion}")
        lines.append("")

        return "\n".join(lines)


@dataclass
class GenerationResult:
    task_id: str
    summary: str
    files_changed: list[str]
    raw_text: str

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            task_id=data["task_id"],
            summary=data["summary"],
            files_changed=data["files_changed"],
            raw_text=data["raw_text"],
        )

    def to_markdown(self) -> str:
        files_lines = "\n".join(f"- `{f}`" for f in self.files_changed)
        return (
            f"## Generation Result for Task `{self.task_id}`\n\n"
            f"### Summary\n\n"
            f"{self.summary}\n\n"
            f"### Files Changed\n\n"
            f"{files_lines}\n"
        )


class Verdict(str, Enum):
    ADVANCE_TASK = "advance_task"
    RETRY_TASK = "retry_task"
    REQUEST_REPLAN = "request_replan"
    HALT_RUN = "halt_run"


@dataclass
class CheckResult:
    name: str                       # e.g., "pytest", "lint", "ai_review"
    passed: bool
    output: str
    severity: str                   # "blocking" | "warning" | "info"

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            name=data["name"],
            passed=data["passed"],
            output=data["output"],
            severity=data["severity"],
        )

    def to_markdown(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"**{self.name}** [{self.severity}]: {status}"]
        if self.output:
            lines.append(f"```\n{self.output}\n```")
        return "\n".join(lines)


@dataclass
class EvaluationResult:
    task_id: str
    verdict: Verdict
    scores: dict[str, float]
    check_results: list[CheckResult]
    feedback: str
    replan_reason: str | None
    raw_text: str

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict.value,
            "scores": self.scores,
            "check_results": [c.to_json() for c in self.check_results],
            "feedback": self.feedback,
            "replan_reason": self.replan_reason,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            task_id=data["task_id"],
            verdict=Verdict(data["verdict"]),
            scores=data["scores"],
            check_results=[CheckResult.from_json(c) for c in data["check_results"]],
            feedback=data["feedback"],
            replan_reason=data.get("replan_reason"),
            raw_text=data["raw_text"],
        )

    def to_markdown(self) -> str:
        lines = [
            f"## Evaluation for Task `{self.task_id}`\n",
            f"**Verdict:** `{self.verdict.value}`\n",
        ]

        # Scores table
        if self.scores:
            lines.append("### Scores\n")
            lines.append("| Criterion | Score |")
            lines.append("|-----------|-------|")
            for criterion, score in self.scores.items():
                lines.append(f"| {criterion} | {score:.2f} |")
            lines.append("")

        # Check results
        if self.check_results:
            lines.append("### Checks\n")
            lines.append("| Check | Passed | Severity |")
            lines.append("|-------|--------|----------|")
            for check in self.check_results:
                status = "PASS" if check.passed else "FAIL"
                lines.append(f"| {check.name} | {status} | {check.severity} |")
            lines.append("")

            # Detail for failed checks
            failed = [c for c in self.check_results if not c.passed]
            if failed:
                lines.append("### Failed Check Details\n")
                for check in failed:
                    lines.append(f"**{check.name}** ({check.severity}):")
                    lines.append(f"```\n{check.output}\n```")
                    lines.append("")

        lines.append("### Feedback\n")
        lines.append(f"{self.feedback}\n")

        if self.replan_reason:
            lines.append("### Replan Reason\n")
            lines.append(f"{self.replan_reason}\n")

        return "\n".join(lines)


@dataclass
class UsageInfo:
    input_tokens: int
    output_tokens: int
    spend_usd: float

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Self:
        return cls(
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            spend_usd=data["spend_usd"],
        )


@dataclass
class StageExecution(Generic[T]):
    result: T
    usage: UsageInfo
    session_key: str    # Logical key, e.g. "planner:v1", "generator:task-01"
    session_id: str     # SDK session ID (opaque, for resumption)

    def to_json(self) -> dict:
        result = self.result
        if hasattr(result, "to_json"):
            result_dict = result.to_json()
        else:
            result_dict = asdict(result)
        return {
            "result": result_dict,
            "usage": self.usage.to_json(),
            "session_key": self.session_key,
            "session_id": self.session_id,
        }

    @classmethod
    def from_json(cls, data: dict, result_cls) -> "StageExecution":
        """Reconstruct a StageExecution from JSON.

        Args:
            data: The serialized dict.
            result_cls: The class to use when deserializing ``data["result"]``.
                Must have a ``from_json`` classmethod.
        """
        return cls(
            result=result_cls.from_json(data["result"]),
            usage=UsageInfo.from_json(data["usage"]),
            session_key=data["session_key"],
            session_id=data["session_id"],
        )
