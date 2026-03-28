"""Tests for agent_harness/state.py — RunState and RunStatus."""

import json
import time
from pathlib import Path

import pytest

from agent_harness.state import RunState, RunStatus


# ---------------------------------------------------------------------------
# RunStatus enum
# ---------------------------------------------------------------------------


class TestRunStatus:
    def test_all_six_values_exist(self):
        values = {s.value for s in RunStatus}
        assert values == {"planning", "executing", "completed", "halted", "failed", "paused"}

    def test_serialize_correctly(self):
        assert RunStatus.PLANNING.value == "planning"
        assert RunStatus.EXECUTING.value == "executing"
        assert RunStatus.COMPLETED.value == "completed"
        assert RunStatus.HALTED.value == "halted"
        assert RunStatus.FAILED.value == "failed"
        assert RunStatus.PAUSED.value == "paused"

    def test_construct_from_string(self):
        assert RunStatus("planning") == RunStatus.PLANNING
        assert RunStatus("completed") == RunStatus.COMPLETED

    def test_is_str_enum(self):
        # RunStatus inherits from str, so it can be compared to strings
        assert RunStatus.PLANNING == "planning"


# ---------------------------------------------------------------------------
# RunState.new()
# ---------------------------------------------------------------------------


class TestRunStateNew:
    def test_creates_planning_status(self):
        state = RunState.new("run-001")
        assert state.status == RunStatus.PLANNING

    def test_run_id_preserved(self):
        state = RunState.new("my-run-42")
        assert state.run_id == "my-run-42"

    def test_default_values(self):
        state = RunState.new("run-001")
        assert state.status_reason is None
        assert state.current_phase == "planning"
        assert state.current_task_index == 0
        assert state.total_tasks == 0
        assert state.iterations_on_current_task == 0
        assert state.plan_version == 0
        assert state.completed_task_ids == []
        assert state.cumulative_spend_usd == 0.0
        assert state.session_ids == {}
        assert state.artifact_manifest == []

    def test_timestamps_are_set(self):
        state = RunState.new("run-001")
        assert state.started_at is not None
        assert state.updated_at is not None
        assert len(state.started_at) > 0

    def test_started_and_updated_are_equal_on_creation(self):
        state = RunState.new("run-001")
        assert state.started_at == state.updated_at


# ---------------------------------------------------------------------------
# transition()
# ---------------------------------------------------------------------------


class TestTransition:
    def test_status_changes(self):
        state = RunState.new("run-001")
        state.transition(RunStatus.EXECUTING)
        assert state.status == RunStatus.EXECUTING

    def test_reason_is_set(self):
        state = RunState.new("run-001")
        state.transition(RunStatus.PAUSED, reason="budget_exhausted")
        assert state.status_reason == "budget_exhausted"

    def test_reason_defaults_to_none(self):
        state = RunState.new("run-001")
        state.transition(RunStatus.EXECUTING)
        assert state.status_reason is None

    def test_updated_at_changes(self):
        state = RunState.new("run-001")
        before = state.updated_at
        # Small sleep to ensure time advances
        time.sleep(0.01)
        state.transition(RunStatus.EXECUTING)
        assert state.updated_at >= before

    def test_halt_transition(self):
        state = RunState.new("run-001")
        state.transition(RunStatus.HALTED, reason="quality gate failed")
        assert state.status == RunStatus.HALTED
        assert state.status_reason == "quality gate failed"

    def test_completed_transition(self):
        state = RunState.new("run-001")
        state.transition(RunStatus.COMPLETED)
        assert state.status == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# advance_task()
# ---------------------------------------------------------------------------


class TestAdvanceTask:
    def test_index_increments(self):
        state = RunState.new("run-001")
        state.total_tasks = 3
        state.current_task_index = 0
        state.advance_task()
        assert state.current_task_index == 1

    def test_iterations_reset(self):
        state = RunState.new("run-001")
        state.total_tasks = 3
        state.iterations_on_current_task = 5
        state.advance_task()
        assert state.iterations_on_current_task == 0

    def test_phase_updates(self):
        state = RunState.new("run-001")
        state.total_tasks = 3
        state.current_task_index = 0
        state.advance_task()
        assert state.current_phase == "task_1"

    def test_phase_done_when_last_task_completes(self):
        state = RunState.new("run-001")
        state.total_tasks = 2
        state.current_task_index = 1  # last index
        state.advance_task()
        assert state.current_phase == "done"

    def test_index_at_end(self):
        state = RunState.new("run-001")
        state.total_tasks = 1
        state.current_task_index = 0
        state.advance_task()
        assert state.current_task_index == 1
        assert state.current_task_index >= state.total_tasks

    def test_updated_at_changes(self):
        state = RunState.new("run-001")
        state.total_tasks = 3
        before = state.updated_at
        time.sleep(0.01)
        state.advance_task()
        assert state.updated_at >= before

    def test_sequential_advances(self):
        state = RunState.new("run-001")
        state.total_tasks = 3
        state.advance_task()
        assert state.current_phase == "task_1"
        state.advance_task()
        assert state.current_phase == "task_2"
        state.advance_task()
        assert state.current_phase == "done"


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_round_trip_new_state(self, tmp_path):
        state = RunState.new("run-save-load")
        path = tmp_path / "run_state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert loaded == state

    def test_round_trip_after_transition(self, tmp_path):
        state = RunState.new("run-save-load")
        state.transition(RunStatus.EXECUTING, reason="test")
        path = tmp_path / "run_state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert loaded.status == RunStatus.EXECUTING
        assert loaded.status_reason == "test"

    def test_round_trip_after_advance(self, tmp_path):
        state = RunState.new("run-save-load")
        state.total_tasks = 3
        state.advance_task()
        path = tmp_path / "run_state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert loaded.current_task_index == 1
        assert loaded.current_phase == "task_1"

    def test_round_trip_with_session_ids(self, tmp_path):
        state = RunState.new("run-sessions")
        state.session_ids["planner:v1"] = "sess-aaa"
        state.session_ids["generator:t1"] = "sess-bbb"
        path = tmp_path / "run_state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert loaded.session_ids == state.session_ids

    def test_round_trip_with_completed_tasks(self, tmp_path):
        state = RunState.new("run-completed")
        state.completed_task_ids = ["task-1", "task-2"]
        path = tmp_path / "run_state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert loaded.completed_task_ids == ["task-1", "task-2"]

    def test_save_creates_parent_dirs(self, tmp_path):
        state = RunState.new("run-001")
        deep_path = tmp_path / "a" / "b" / "c" / "state.json"
        state.save(deep_path)
        assert deep_path.exists()

    def test_saved_file_is_valid_json(self, tmp_path):
        state = RunState.new("run-001")
        path = tmp_path / "state.json"
        state.save(path)
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data["run_id"] == "run-001"
        assert data["status"] == "planning"

    def test_status_is_enum_after_load(self, tmp_path):
        state = RunState.new("run-001")
        path = tmp_path / "state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert isinstance(loaded.status, RunStatus)
        assert loaded.status == RunStatus.PLANNING

    def test_full_state_equality(self, tmp_path):
        """Create a state with many fields populated, save, load, and check equality."""
        state = RunState.new("run-full")
        state.transition(RunStatus.EXECUTING)
        state.total_tasks = 5
        state.current_task_index = 2
        state.iterations_on_current_task = 1
        state.plan_version = 2
        state.completed_task_ids = ["t0", "t1"]
        state.cumulative_spend_usd = 1.23
        state.session_ids = {"planner:v2": "s1", "generator:t2": "s2"}
        state.artifact_manifest = ["/tmp/plan.json", "/tmp/plan.md"]

        path = tmp_path / "state.json"
        state.save(path)
        loaded = RunState.load(path)
        assert loaded == state
