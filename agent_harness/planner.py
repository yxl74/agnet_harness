"""DefaultPlanner: SDK-backed Planner implementation for the agent harness."""

from __future__ import annotations

import json
import re
import sys

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
# Structured output schema
# ---------------------------------------------------------------------------

PLAN_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "spec": {"type": "string"},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "contract": {
                            "type": "object",
                            "properties": {
                                "success_criteria": {"type": "array", "items": {"type": "string"}},
                                "scope_boundaries": {"type": "string"},
                            },
                            "required": ["success_criteria", "scope_boundaries"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["id", "title", "description", "acceptance_criteria", "dependencies", "contract"],
                    "additionalProperties": False,
                },
            },
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["spec", "tasks", "acceptance_criteria"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# DefaultPlanner
# ---------------------------------------------------------------------------


class DefaultPlanner:
    """Planner backed by the Claude Agent SDK.

    Wraps ``claude_agent_sdk.query()`` to satisfy the ``Planner`` protocol
    defined in ``agent_harness.core``.

    When ``structured_output=True`` (default), the planner uses
    ``output_format=PLAN_SCHEMA`` so the agent SDK returns schema-validated
    JSON directly, eliminating regex-based parsing failures.

    When ``structured_output=False``, the legacy ``_parse_plan()`` regex
    approach is used (for backwards compatibility with older configs).
    """

    def __init__(
        self,
        system_prompt: str,
        tools: list[str],
        model: str,
        cwd: str,
        structured_output: bool = True,
        effort: str = "high",
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.cwd = cwd
        self.structured_output = structured_output
        self.effort = effort
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
        structured_data: dict | None = None
        session_id = ""

        if not self.structured_output:
            print(
                "WARNING: Using legacy regex parser. Set structured_output=True for production.",
                file=sys.stderr,
            )

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
                permission_mode="bypassPermissions",
                effort=self.effort,
                thinking={"type": "adaptive"},
            )
            if self.structured_output:
                options_kwargs["output_format"] = PLAN_SCHEMA

            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(**options_kwargs),
            ):
                if isinstance(message, ResultMessage):
                    result_text = message.result or ""
                    # SDK populates structured_output with parsed JSON when output_format is set
                    if self.structured_output and message.structured_output is not None:
                        structured_data = message.structured_output
                    # ResultMessage has session_id directly as a field
                    session_id = message.session_id
                    # Use SDK-provided aggregated usage instead of manual summation
                    if message.usage:
                        total_usage.input_tokens = message.usage.get("input_tokens", 0)
                        total_usage.output_tokens = message.usage.get("output_tokens", 0)
                    if message.total_cost_usd is not None:
                        total_usage.spend_usd = message.total_cost_usd
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    # Also capture session_id from init message as early signal
                    if "session_id" in message.data:
                        session_id = message.data["session_id"]

        except ImportError:
            # SDK not installed — callers using mocks should patch query directly
            result_text = ""

        if self.structured_output:
            if structured_data is not None:
                plan = self._parse_plan_from_dict(structured_data, task, result_text)
            else:
                # Fallback: try parsing result_text as JSON
                plan = self._parse_plan_structured(result_text, task)
        else:
            plan = self._parse_plan(result_text, task)

        # Only compute cost if SDK didn't already provide it
        if total_usage.spend_usd == 0.0:
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
        if self.structured_output:
            lines = [
                f"Create a detailed plan for: {task}",
                "",
                "Your response will be structured JSON. Populate these fields:",
                '  "spec": brief product/spec context',
                '  "acceptance_criteria": list of overall acceptance criteria for the run',
                '  "tasks": array of task objects, each with:',
                '    "id": sequential identifier like "task-01"',
                '    "title": short imperative title',
                '    "description": what to build (specific enough for the Generator)',
                '    "acceptance_criteria": per-task verifiable criteria',
                '    "dependencies": list of task IDs this task depends on (or empty)',
                '    "contract":',
                '      "success_criteria": list of verifiable pass/fail criteria',
                '      "scope_boundaries": what is in and out of scope',
            ]
        else:
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

    def _parse_plan_from_dict(self, data: dict, task_description: str, raw_text: str) -> Plan:
        """Construct a Plan from a pre-parsed dict (from SDK structured_output field)."""
        tasks = [
            Task(
                id=t.get("id", f"task-{i+1:02d}"),
                title=t.get("title", f"Task {i+1}"),
                description=t.get("description", ""),
                acceptance_criteria=t.get("acceptance_criteria", []),
                dependencies=t.get("dependencies", []),
                contract=Contract(
                    success_criteria=t.get("contract", {}).get("success_criteria", []),
                    scope_boundaries=t.get("contract", {}).get("scope_boundaries", ""),
                ),
            )
            for i, t in enumerate(data.get("tasks", []))
        ]
        return Plan(
            task_description=task_description,
            spec=data.get("spec", ""),
            tasks=tasks,
            acceptance_criteria=data.get("acceptance_criteria", []),
            raw_text=raw_text,
        )

    def _parse_plan_structured(self, text: str, task_description: str) -> Plan:
        """Parse a Plan from a schema-validated JSON string returned by the SDK.

        Raises:
            ValueError: if ``text`` is not valid JSON — callers should treat
                this as a hard error, not fall back silently.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Planner returned invalid JSON (structured_output=True). "
                f"Raw text: {text[:200]!r}"
            ) from exc

        tasks = [
            Task(
                id=t.get("id", f"task-{i+1:02d}"),
                title=t.get("title", f"Task {i+1}"),
                description=t.get("description", ""),
                acceptance_criteria=t.get("acceptance_criteria", []),
                dependencies=t.get("dependencies", []),
                contract=Contract(
                    success_criteria=t.get("contract", {}).get("success_criteria", []),
                    scope_boundaries=t.get("contract", {}).get("scope_boundaries", ""),
                ),
            )
            for i, t in enumerate(data.get("tasks", []))
        ]
        return Plan(
            task_description=task_description,
            spec=data.get("spec", ""),
            tasks=tasks,
            acceptance_criteria=data.get("acceptance_criteria", []),
            raw_text=text,
        )

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
