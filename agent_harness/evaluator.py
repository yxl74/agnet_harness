"""DefaultEvaluator: SDK-backed Evaluator implementation for the agent harness."""

from __future__ import annotations

import json
import re
import sys

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
# Structured output schema
# ---------------------------------------------------------------------------

EVALUATION_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "severity": {"type": "string"},
                        "output": {"type": "string"},
                    },
                    "required": ["name", "passed", "severity", "output"],
                    "additionalProperties": False,
                },
            },
            "scores": {
                "type": "object",
            },
            "verdict": {
                "type": "string",
                "enum": ["ADVANCE_TASK", "RETRY_TASK", "REQUEST_REPLAN", "HALT_RUN"],
            },
            "feedback": {"type": "string"},
            "replan_reason": {"type": ["string", "null"]},
        },
        "required": ["checks", "scores", "verdict", "feedback"],
        "additionalProperties": False,
    },
}

_VERDICT_MAP = {
    "ADVANCE_TASK": Verdict.ADVANCE_TASK,
    "RETRY_TASK": Verdict.RETRY_TASK,
    "REQUEST_REPLAN": Verdict.REQUEST_REPLAN,
    "HALT_RUN": Verdict.HALT_RUN,
}


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

    When ``structured_output=True`` (default), the evaluator uses
    ``output_format=EVALUATION_SCHEMA`` so the agent SDK returns
    schema-validated JSON, eliminating regex parsing ambiguity.

    When ``structured_output=False``, the legacy ``_parse_evaluation()`` regex
    approach is used (for backwards compatibility with older configs).
    """

    def __init__(
        self,
        system_prompt: str,
        tools: list[str],
        model: str,
        cwd: str,
        structured_output: bool = True,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.cwd = cwd
        self.structured_output = structured_output
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

        if not self.structured_output:
            print(
                "WARNING: Using legacy regex parser. Set structured_output=True for production.",
                file=sys.stderr,
            )

        try:
            from claude_agent_sdk import (  # type: ignore[import]
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                query,
            )

            options_kwargs: dict = dict(
                system_prompt=self.system_prompt,
                allowed_tools=self.tools,
                model=self.model,
                cwd=cwd,
                permission_mode="bypassPermissions",
            )
            if self.structured_output:
                options_kwargs["output_format"] = EVALUATION_SCHEMA

            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(**options_kwargs),
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

        if self.structured_output:
            eval_result = self._parse_evaluation_structured(result_text, task.id)
        else:
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

        if self.structured_output:
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
                "3. **Populate the structured output fields:**",
                "",
                "   - `checks`: array of check results, each with:",
                "     - `name`: tool name (e.g. \"pytest\", \"ruff\")",
                "     - `passed`: true if the check passed, false otherwise",
                "     - `severity`: \"blocking\" or \"warning\"",
                "     - `output`: brief summary of the check output",
                "   - `scores`: object mapping dimension names to floats 0.0–1.0",
                "     (e.g. {\"correctness\": 0.9, \"code_quality\": 0.8, \"spec_adherence\": 0.85})",
                "   - `verdict`: one of ADVANCE_TASK, RETRY_TASK, REQUEST_REPLAN, HALT_RUN",
                "   - `feedback`: actionable feedback for the Generator (required for RETRY_TASK)",
                "   - `replan_reason`: reason the contract itself is flawed (required for REQUEST_REPLAN, null otherwise)",
                "",
                "**Verdict definitions:**",
                "- ADVANCE_TASK — all blocking checks pass and all success criteria are met.",
                "- RETRY_TASK — checks fail or criteria unmet, but the contract is valid.",
                "- REQUEST_REPLAN — the contract itself is flawed or contradictory.",
                "- HALT_RUN — quality is irrecoverable or further iteration is futile.",
            ]
        else:
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

    def _parse_evaluation_structured(self, text: str, task_id: str) -> EvaluationResult:
        """Parse an EvaluationResult from a schema-validated JSON string.

        On JSON parse failure, returns a RETRY_TASK result with an explanatory
        feedback message rather than silently defaulting.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return EvaluationResult(
                task_id=task_id,
                verdict=Verdict.RETRY_TASK,
                scores={},
                check_results=[],
                feedback=(
                    f"Evaluator returned invalid JSON (structured_output=True). "
                    f"Raw text: {text[:200]!r}"
                ),
                replan_reason=None,
                raw_text=text,
            )

        check_results = [
            CheckResult(
                name=c.get("name", ""),
                passed=bool(c.get("passed", False)),
                severity=c.get("severity", "blocking"),
                output=c.get("output", ""),
            )
            for c in data.get("checks", [])
        ]

        # scores values may be int or float in JSON; coerce to float
        raw_scores = data.get("scores", {})
        scores: dict[str, float] = {}
        if isinstance(raw_scores, dict):
            for k, v in raw_scores.items():
                try:
                    scores[k] = float(v)
                except (TypeError, ValueError):
                    pass

        verdict_str = data.get("verdict", "RETRY_TASK").upper()
        verdict = _VERDICT_MAP.get(verdict_str, Verdict.RETRY_TASK)

        return EvaluationResult(
            task_id=task_id,
            verdict=verdict,
            scores=scores,
            check_results=check_results,
            feedback=data.get("feedback", ""),
            replan_reason=data.get("replan_reason"),
            raw_text=text,
        )

    def _parse_evaluation(self, text: str, task_id: str) -> EvaluationResult:
        """Extract an EvaluationResult from the agent's response text."""
        check_results: list[CheckResult] = []
        scores: dict[str, float] = {}
        verdict: Verdict = Verdict.RETRY_TASK  # safe default
        feedback = ""
        replan_reason: str | None = None

        # Locate the EVALUATION RESULT block — tolerant of formatting variation
        block_match = re.search(
            r"##\s+EVALUATION\s+RESULT\s*\n([\s\S]*?)(?:^```|\Z)",
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

        # Parse VERDICT — handle both "VERDICT: X" and "## Verdict\nX" formats
        verdict_match = re.search(r"VERDICT:\s*(\S+)", block, re.IGNORECASE)
        if not verdict_match:
            # Fallback: look for verdict keyword on its own line (agent may use markdown headings)
            verdict_match = re.search(
                r"(?:^|\n)\s*(ADVANCE_TASK|RETRY_TASK|REQUEST_REPLAN|HALT_RUN)\s*(?:\n|$)",
                block,
                re.IGNORECASE,
            )
        if verdict_match:
            raw_verdict = verdict_match.group(1).strip().upper()
            verdict = _VERDICT_MAP.get(raw_verdict, Verdict.RETRY_TASK)

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
