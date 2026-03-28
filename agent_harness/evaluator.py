"""DefaultEvaluator: SDK-backed Evaluator implementation for the agent harness."""

from __future__ import annotations

import json
import re

from agent_harness.artifacts import (
    CheckResult,
    EvaluationResult,
    GenerationResult,
    StageExecution,
    Task,
    UsageInfo,
    Verdict,
)
from agent_harness.planner import _compute_cost


# ---------------------------------------------------------------------------
# DefaultEvaluator
# ---------------------------------------------------------------------------


class DefaultEvaluator:
    """Evaluator backed by the Claude Agent SDK.

    Wraps ``claude_agent_sdk.query()`` to satisfy the ``Evaluator`` protocol
    defined in ``agent_harness.core``.

    The evaluator composes three concerns in a single agent call:

    1. **Deterministic checks** — the agent is instructed to run test commands
       (e.g. ``pytest``, ``ruff``, ``mypy``) and report structured results.
    2. **AI review** — the agent reviews code quality, spec adherence, and
       security concerns, emitting per-criterion scores.
    3. **Verdict** — the agent emits exactly one of:
       ADVANCE_TASK | RETRY_TASK | REQUEST_REPLAN | HALT_RUN.

    The agent always gets a fresh session (no long-lived session state) so
    evaluations are independent and reproducible.
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
        self._eval_count = 0

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        task: Task,
        result: GenerationResult,
        cwd: str,
    ) -> StageExecution[EvaluationResult]:
        """Evaluate *result* against *task*'s contract.

        Args:
            task: The task that was just generated.
            result: The generation output to evaluate.
            cwd: Working directory; the agent runs checks here.

        Returns:
            A :class:`StageExecution` wrapping the parsed :class:`EvaluationResult`.
        """
        self._eval_count += 1
        session_key = f"evaluator:{task.id}:iter-{self._eval_count}"

        prompt = self._build_prompt(task, result)

        total_usage = UsageInfo(input_tokens=0, output_tokens=0, spend_usd=0.0)
        result_text = ""
        session_id = ""

        try:
            from claude_agent_sdk import (  # type: ignore[import]
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                query,
            )

            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    system_prompt=self.system_prompt,
                    allowed_tools=self.tools,
                    model=self.model,
                    cwd=cwd,
                    permission_mode="bypassPermissions",
                ),
            ):
                if isinstance(message, ResultMessage):
                    result_text = message.result or ""
                elif isinstance(message, AssistantMessage):
                    if message.usage:
                        total_usage.input_tokens += message.usage.get(
                            "input_tokens", 0
                        )
                        total_usage.output_tokens += message.usage.get(
                            "output_tokens", 0
                        )
                if (
                    hasattr(message, "data")
                    and message.data
                    and "session_id" in message.data
                ):
                    session_id = message.data["session_id"]

        except ImportError:
            result_text = ""

        eval_result = self._parse_evaluation(result_text, task.id)
        total_usage.spend_usd = _compute_cost(self.model, total_usage)

        return StageExecution(
            result=eval_result,
            usage=total_usage,
            session_key=session_key,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, task: Task, result: GenerationResult) -> str:
        lines = [
            "## Evaluation Request",
            "",
            f"**Task:** {task.title} (id: `{task.id}`)",
            "",
            "### Task Description",
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
            "",
            "### Generation Summary",
            result.summary or "(no summary provided)",
        ]
        if result.files_changed:
            lines += ["", "### Files Changed"]
            for f in result.files_changed:
                lines.append(f"- `{f}`")

        lines += [
            "",
            "---",
            "## Instructions",
            "",
            "1. **Run deterministic checks** using your Bash tool:",
            "   - If tests exist: run `pytest` (or the project test command).",
            "   - Run the linter if present (e.g. `ruff check .` or `flake8`).",
            "   - Run the type checker if present (e.g. `mypy .` or `pyright`).",
            "   For each check, note the exit code and relevant output.",
            "",
            "2. **Review the code** for quality, contract adherence, and security.",
            "",
            "3. **Output a structured EVALUATION RESULT block** in this exact format:",
            "",
            "```",
            "## EVALUATION RESULT",
            "",
            "### Checks",
            "CHECK: pytest | passed: true | severity: blocking | output: <summary>",
            "CHECK: ruff | passed: false | severity: blocking | output: <errors>",
            "",
            "### Scores",
            "SCORE: correctness | 0.85",
            "SCORE: code_quality | 0.90",
            "SCORE: security | 1.00",
            "SCORE: spec_adherence | 0.75",
            "",
            "### Verdict",
            "VERDICT: ADVANCE_TASK",
            "",
            "### Feedback",
            "FEEDBACK: <actionable feedback>",
            "",
            "### Replan Reason",
            "REPLAN_REASON: <only if verdict is REQUEST_REPLAN, else omit>",
            "```",
            "",
            "Valid verdicts: ADVANCE_TASK, RETRY_TASK, REQUEST_REPLAN, HALT_RUN",
            "",
            "- Emit ADVANCE_TASK only when ALL blocking checks pass.",
            "- Emit RETRY_TASK when checks fail but the task is fixable.",
            "- Emit REQUEST_REPLAN when the contract itself is wrong or contradictory.",
            "- Emit HALT_RUN when further iteration is futile.",
        ]
        return "\n".join(lines)

    def _parse_evaluation(self, text: str, task_id: str) -> EvaluationResult:
        """Extract an EvaluationResult from the agent's response text."""
        check_results: list[CheckResult] = []
        scores: dict[str, float] = {}
        verdict: Verdict = Verdict.RETRY_TASK  # safe default
        feedback = ""
        replan_reason: str | None = None

        # Locate the EVALUATION RESULT block
        block_match = re.search(
            r"##\s+EVALUATION RESULT\s*\n([\s\S]*?)(?:^```|$)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        block = block_match.group(1) if block_match else text

        # Parse CHECK lines
        for m in re.finditer(
            r"CHECK:\s*(\S+)\s*\|\s*passed:\s*(true|false)\s*\|\s*severity:\s*(\S+)\s*\|\s*output:\s*(.+)",
            block,
            re.IGNORECASE,
        ):
            name = m.group(1).strip().rstrip("|").strip()
            passed = m.group(2).strip().lower() == "true"
            severity = m.group(3).strip().rstrip("|").strip()
            output = m.group(4).strip()
            check_results.append(
                CheckResult(name=name, passed=passed, output=output, severity=severity)
            )

        # Parse SCORE lines
        for m in re.finditer(
            r"SCORE:\s*(\S+)\s*\|\s*([\d.]+)",
            block,
            re.IGNORECASE,
        ):
            criterion = m.group(1).strip().rstrip("|").strip()
            try:
                scores[criterion] = float(m.group(2))
            except ValueError:
                pass

        # Parse VERDICT
        verdict_match = re.search(r"VERDICT:\s*(\S+)", block, re.IGNORECASE)
        if verdict_match:
            raw_verdict = verdict_match.group(1).strip().upper()
            verdict_map = {
                "ADVANCE_TASK": Verdict.ADVANCE_TASK,
                "RETRY_TASK": Verdict.RETRY_TASK,
                "REQUEST_REPLAN": Verdict.REQUEST_REPLAN,
                "HALT_RUN": Verdict.HALT_RUN,
            }
            verdict = verdict_map.get(raw_verdict, Verdict.RETRY_TASK)

        # Parse FEEDBACK
        feedback_match = re.search(r"FEEDBACK:\s*(.+)", block, re.IGNORECASE)
        if feedback_match:
            feedback = feedback_match.group(1).strip()

        # Parse REPLAN_REASON
        replan_match = re.search(r"REPLAN_REASON:\s*(.+)", block, re.IGNORECASE)
        if replan_match:
            replan_reason = replan_match.group(1).strip()

        # If parsing totally fails, fall back to RETRY_TASK with raw text feedback
        if not feedback and not check_results:
            # Try to find any useful content
            feedback = text[:500] if text else "Evaluation failed to produce structured output."

        return EvaluationResult(
            task_id=task_id,
            verdict=verdict,
            scores=scores,
            check_results=check_results,
            feedback=feedback,
            replan_reason=replan_reason,
            raw_text=text,
        )
