"""DefaultPlanner: SDK-backed Planner implementation for the agent harness."""

from __future__ import annotations

import json
import re

from agent_harness.artifacts import (
    Contract,
    Plan,
    StageExecution,
    Task,
    UsageInfo,
)


# ---------------------------------------------------------------------------
# Per-model pricing (USD per token)
# ---------------------------------------------------------------------------

# (input_usd_per_1k, output_usd_per_1k)
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-6":      (0.015, 0.075),
    "claude-opus-4-5":      (0.015, 0.075),
    "claude-sonnet-4-6":    (0.003, 0.015),
    "claude-sonnet-4-5":    (0.003, 0.015),
    "claude-haiku-4-5":     (0.00025, 0.00125),
    # Fallback for unknown models
    "default":              (0.003, 0.015),
}


def _compute_cost(model: str, usage: UsageInfo) -> float:
    """Return estimated USD cost for the given token usage and model."""
    pricing = _MODEL_PRICING.get(model, _MODEL_PRICING["default"])
    input_cost = (usage.input_tokens / 1000) * pricing[0]
    output_cost = (usage.output_tokens / 1000) * pricing[1]
    return round(input_cost + output_cost, 6)


# ---------------------------------------------------------------------------
# DefaultPlanner
# ---------------------------------------------------------------------------


class DefaultPlanner:
    """Planner backed by the Claude Agent SDK.

    Wraps ``claude_agent_sdk.query()`` to satisfy the ``Planner`` protocol
    defined in ``agent_harness.core``.

    The planner is instructed via its system prompt to produce a structured
    JSON block inside its response.  The ``_parse_plan`` method extracts that
    JSON block and falls back to a minimal single-task plan if parsing fails.
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
        self._plan_version = 0

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def plan(
        self,
        task: str,
        cwd: str,
        replan_context: str | None = None,
        completed_task_ids: list[str] | None = None,
    ) -> StageExecution[Plan]:
        """Call the agent SDK to produce a Plan for *task*.

        Args:
            task: High-level task description.
            cwd: Working directory for the agent.
            replan_context: When replanning, the reason why.
            completed_task_ids: Task IDs already completed; agent should not
                re-include them in the new plan.

        Returns:
            A :class:`StageExecution` wrapping the parsed :class:`Plan`.
        """
        prompt = self._build_prompt(task, replan_context, completed_task_ids)
        self._plan_version += 1
        session_key = f"planner:v{self._plan_version}"

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
                # Capture session_id from init message
                if (
                    hasattr(message, "data")
                    and message.data
                    and "session_id" in message.data
                ):
                    session_id = message.data["session_id"]

        except ImportError:
            # SDK not installed — callers using mocks should patch query directly
            result_text = ""

        plan = self._parse_plan(result_text, task)
        total_usage.spend_usd = _compute_cost(self.model, total_usage)

        return StageExecution(
            result=plan,
            usage=total_usage,
            session_key=session_key,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        task: str,
        replan_context: str | None,
        completed_task_ids: list[str] | None,
    ) -> str:
        lines = [
            f"Create a detailed plan for: {task}",
            "",
            "Output your plan as a JSON block fenced with ```json ... ``` containing:",
            "{",
            '  "spec": "brief product/spec context",',
            '  "acceptance_criteria": ["overall criterion 1", "..."],',
            '  "tasks": [',
            "    {",
            '      "id": "task-01",',
            '      "title": "Short title",',
            '      "description": "What to build",',
            '      "acceptance_criteria": ["..."],',
            '      "dependencies": [],',
            '      "contract": {',
            '        "success_criteria": ["..."],',
            '        "scope_boundaries": "..."',
            "      }",
            "    }",
            "  ]",
            "}",
        ]

        if replan_context:
            lines += [
                "",
                f"REPLAN CONTEXT: {replan_context}",
            ]
        if completed_task_ids:
            lines += [
                "",
                "Already completed tasks (do not repeat): "
                + ", ".join(completed_task_ids),
            ]

        return "\n".join(lines)

    def _parse_plan(self, text: str, task_description: str) -> Plan:
        """Extract a Plan from the agent's response.

        Looks for a ```json ... ``` block first, then falls back to a
        minimal single-task stub so the orchestrator can always proceed.
        """
        data: dict | None = None

        # Prefer fenced JSON block
        match = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
        if match:
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                data = None

        # Fall back to first standalone JSON object
        if data is None:
            brace_match = re.search(r"\{[\s\S]*\}", text)
            if brace_match:
                try:
                    data = json.loads(brace_match.group(0))
                except json.JSONDecodeError:
                    data = None

        if data and "tasks" in data:
            tasks = [
                Task(
                    id=t.get("id", f"task-{i+1:02d}"),
                    title=t.get("title", f"Task {i+1}"),
                    description=t.get("description", ""),
                    acceptance_criteria=t.get("acceptance_criteria", []),
                    dependencies=t.get("dependencies", []),
                    contract=Contract(
                        success_criteria=t.get("contract", {}).get(
                            "success_criteria", []
                        ),
                        scope_boundaries=t.get("contract", {}).get(
                            "scope_boundaries", ""
                        ),
                    ),
                )
                for i, t in enumerate(data["tasks"])
            ]
            return Plan(
                task_description=task_description,
                spec=data.get("spec", ""),
                tasks=tasks,
                acceptance_criteria=data.get("acceptance_criteria", []),
                raw_text=text,
            )

        # Fallback: single-task stub so the orchestrator never gets None
        return Plan(
            task_description=task_description,
            spec="",
            tasks=[
                Task(
                    id="task-01",
                    title=task_description[:80],
                    description=task_description,
                    acceptance_criteria=[],
                    dependencies=[],
                    contract=Contract(
                        success_criteria=[],
                        scope_boundaries="Full task scope",
                    ),
                )
            ],
            acceptance_criteria=[],
            raw_text=text,
        )
