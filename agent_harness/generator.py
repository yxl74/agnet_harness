"""DefaultGenerator: SDK-backed Generator implementation for the agent harness."""

from __future__ import annotations

import re

from agent_harness.artifacts import (
    EvaluationResult,
    GenerationResult,
    StageExecution,
    Task,
    UsageInfo,
)
from agent_harness.planner import _compute_cost


# ---------------------------------------------------------------------------
# DefaultGenerator
# ---------------------------------------------------------------------------


class DefaultGenerator:
    """Generator backed by the Claude Agent SDK.

    Wraps ``claude_agent_sdk.query()`` to satisfy the ``Generator`` protocol
    defined in ``agent_harness.core``.

    Session management:
    - A long-lived session is maintained per task (``self._session_id``).
    - On the first call for a task, a fresh session is started.
    - On retries for the same task, ``resume=self._session_id`` is passed so
      the agent retains full context of what it has already done.
    - When a new task begins (different ``task.id``), the session is reset.
    """

    def __init__(
        self,
        system_prompt: str,
        tools: list[str],
        model: str,
        cwd: str,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.cwd = cwd
        self._session_id: str | None = None
        self._current_task_id: str | None = None
        self._task_iteration = 0

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def generate(
        self,
        task: Task,
        prior_evaluation: EvaluationResult | None,
        cwd: str,
    ) -> StageExecution[GenerationResult]:
        """Implement *task*, optionally using *prior_evaluation* for retries.

        Args:
            task: The task to implement.
            prior_evaluation: Previous evaluation result when retrying; None
                on first attempt.
            cwd: Working directory for the agent.

        Returns:
            A :class:`StageExecution` wrapping the parsed :class:`GenerationResult`.
        """
        # Reset session when switching to a new task
        if task.id != self._current_task_id:
            self._session_id = None
            self._current_task_id = task.id
            self._task_iteration = 0

        session_key = f"generator:{task.id}:iter-{self._task_iteration}"
        self._task_iteration += 1

        prompt = self._build_prompt(task, prior_evaluation)

        total_usage = UsageInfo(input_tokens=0, output_tokens=0, spend_usd=0.0)
        result_text = ""
        session_id = self._session_id or ""

        try:
            from claude_agent_sdk import (  # type: ignore[import]
                ClaudeAgentOptions,
                ResultMessage,
                SystemMessage,
                query,
            )

            options_kwargs: dict = dict(
                system_prompt=self.system_prompt,
                allowed_tools=self.tools,
                model=self.model,
                cwd=cwd,
                permission_mode="acceptEdits",
            )
            # Resume the same session when retrying
            if self._session_id:
                options_kwargs["resume"] = self._session_id

            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(**options_kwargs),
            ):
                if isinstance(message, ResultMessage):
                    result_text = message.result or ""
                    # ResultMessage has session_id directly as a field
                    session_id = message.session_id
                    self._session_id = session_id  # persist for retries
                    # Use SDK-provided aggregated usage
                    if message.usage:
                        total_usage.input_tokens = message.usage.get("input_tokens", 0)
                        total_usage.output_tokens = message.usage.get("output_tokens", 0)
                    if message.total_cost_usd is not None:
                        total_usage.spend_usd = message.total_cost_usd
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    if "session_id" in message.data:
                        session_id = message.data["session_id"]
                        self._session_id = session_id

        except ImportError:
            result_text = ""

        gen_result = self._parse_result(result_text, task)
        # Only compute cost if SDK didn't already provide it
        if total_usage.spend_usd == 0.0:
            total_usage.spend_usd = _compute_cost(self.model, total_usage)

        return StageExecution(
            result=gen_result,
            usage=total_usage,
            session_key=session_key,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        task: Task,
        prior_evaluation: EvaluationResult | None,
    ) -> str:
        lines = [
            f"## Task: {task.title} (id: {task.id})",
            "",
            task.description,
            "",
            "### Contract — Success Criteria",
        ]
        for criterion in task.contract.success_criteria:
            lines.append(f"- {criterion}")
        lines += [
            "",
            f"### Contract — Scope Boundaries",
            task.contract.scope_boundaries,
        ]

        if task.acceptance_criteria:
            lines += ["", "### Acceptance Criteria"]
            for criterion in task.acceptance_criteria:
                lines.append(f"- {criterion}")

        if prior_evaluation is not None:
            lines += [
                "",
                "---",
                "## Prior Evaluation — Retry Context",
                "",
                f"**Verdict:** {prior_evaluation.verdict.value}",
                "",
                f"**Feedback:** {prior_evaluation.feedback}",
            ]
            if prior_evaluation.check_results:
                lines += ["", "### Failed Checks"]
                for check in prior_evaluation.check_results:
                    if not check.passed:
                        lines.append(f"- **{check.name}** [{check.severity}]:")
                        if check.output:
                            lines.append(f"  ```\n  {check.output}\n  ```")
            if prior_evaluation.scores:
                lines += ["", "### Scores from Prior Evaluation"]
                for criterion, score in prior_evaluation.scores.items():
                    lines.append(f"- {criterion}: {score:.2f}")

        lines += [
            "",
            "---",
            "When done, output a summary section:",
            "```",
            "## GENERATION SUMMARY",
            "Files changed: file1.py, file2.py",
            "Summary: <what you did and why>",
            "```",
        ]
        return "\n".join(lines)

    def _parse_result(self, text: str, task: Task) -> GenerationResult:
        """Extract a GenerationResult from the agent response text."""
        summary = ""
        files_changed: list[str] = []

        # Look for the GENERATION SUMMARY block
        summary_match = re.search(
            r"##\s+GENERATION SUMMARY\s*\n([\s\S]*?)(?:```|$)",
            text,
            re.IGNORECASE,
        )
        if summary_match:
            block = summary_match.group(1).strip()
            for line in block.splitlines():
                line = line.strip()
                if line.lower().startswith("files changed:"):
                    raw_files = line[len("files changed:"):].strip()
                    files_changed = [f.strip() for f in raw_files.split(",") if f.strip()]
                elif line.lower().startswith("summary:"):
                    summary = line[len("summary:"):].strip()

        # Fallback: extract filenames from code blocks / tool calls mentioning files
        if not files_changed:
            files_changed = re.findall(
                r"(?:created?|modified?|wrote?|edited?|updated?)\s+[`'\"]?([\w./\-]+\.\w+)[`'\"]?",
                text,
                re.IGNORECASE,
            )
            # De-duplicate while preserving order
            seen: set[str] = set()
            deduped: list[str] = []
            for f in files_changed:
                if f not in seen:
                    seen.add(f)
                    deduped.append(f)
            files_changed = deduped

        if not summary:
            # Use the first non-empty paragraph as fallback summary
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            summary = paragraphs[0][:500] if paragraphs else ""

        return GenerationResult(
            task_id=task.id,
            summary=summary,
            files_changed=files_changed,
            raw_text=text,
        )
