"""Tests for agent_harness/config.py — HarnessConfig scaffold and load."""

import json
from pathlib import Path

import pytest

from agent_harness.config import HarnessConfig


# ---------------------------------------------------------------------------
# scaffold_project()
# ---------------------------------------------------------------------------


class TestScaffoldProject:
    def test_creates_config_json(self, tmp_path):
        project_dir = tmp_path / "my-project"
        HarnessConfig.scaffold_project(project_dir, name="my-project")
        assert (project_dir / "config.json").exists()

    def test_creates_prompts_dir(self, tmp_path):
        project_dir = tmp_path / "test-proj"
        HarnessConfig.scaffold_project(project_dir, name="test-proj")
        assert (project_dir / "prompts").is_dir()

    def test_creates_all_prompt_files(self, tmp_path):
        project_dir = tmp_path / "test-proj"
        HarnessConfig.scaffold_project(project_dir, name="test-proj")
        for filename in ("planner.md", "generator.md", "evaluator.md"):
            assert (project_dir / "prompts" / filename).exists()

    def test_creates_runs_dir(self, tmp_path):
        project_dir = tmp_path / "test-proj"
        HarnessConfig.scaffold_project(project_dir, name="test-proj")
        assert (project_dir / "runs").is_dir()

    def test_config_json_has_name(self, tmp_path):
        project_dir = tmp_path / "named-proj"
        HarnessConfig.scaffold_project(project_dir, name="named-proj")
        data = json.loads((project_dir / "config.json").read_text())
        assert data["name"] == "named-proj"

    def test_config_json_has_expected_keys(self, tmp_path):
        project_dir = tmp_path / "test-proj"
        HarnessConfig.scaffold_project(project_dir, name="test-proj")
        data = json.loads((project_dir / "config.json").read_text())
        required_keys = {"name", "model", "max_budget_usd", "generator_tools", "evaluator_tools", "planner_tools"}
        assert required_keys.issubset(data.keys())

    def test_prompt_files_are_non_empty(self, tmp_path):
        project_dir = tmp_path / "test-proj"
        HarnessConfig.scaffold_project(project_dir, name="test-proj")
        for filename in ("planner.md", "generator.md", "evaluator.md"):
            content = (project_dir / "prompts" / filename).read_text()
            assert len(content.strip()) > 0

    def test_creates_project_dir_if_missing(self, tmp_path):
        project_dir = tmp_path / "new" / "nested" / "project"
        assert not project_dir.exists()
        HarnessConfig.scaffold_project(project_dir, name="nested")
        assert project_dir.exists()

    def test_idempotent_no_error_on_second_call(self, tmp_path):
        project_dir = tmp_path / "idempotent-proj"
        HarnessConfig.scaffold_project(project_dir, name="idempotent-proj")
        # Should not raise
        HarnessConfig.scaffold_project(project_dir, name="idempotent-proj")

    def test_idempotent_files_unchanged(self, tmp_path):
        project_dir = tmp_path / "idempotent-proj"
        HarnessConfig.scaffold_project(project_dir, name="idempotent-proj")

        # Read content after first scaffold
        config_content = (project_dir / "config.json").read_text()
        planner_content = (project_dir / "prompts" / "planner.md").read_text()

        # Second call
        HarnessConfig.scaffold_project(project_dir, name="idempotent-proj")

        # Content must be unchanged
        assert (project_dir / "config.json").read_text() == config_content
        assert (project_dir / "prompts" / "planner.md").read_text() == planner_content

    def test_idempotent_does_not_overwrite_custom_config(self, tmp_path):
        project_dir = tmp_path / "custom-proj"
        HarnessConfig.scaffold_project(project_dir, name="custom-proj")

        # Modify config to have a custom value
        config_path = project_dir / "config.json"
        data = json.loads(config_path.read_text())
        data["max_budget_usd"] = 999.0
        config_path.write_text(json.dumps(data))

        # Re-scaffold should not overwrite
        HarnessConfig.scaffold_project(project_dir, name="custom-proj")
        reloaded = json.loads(config_path.read_text())
        assert reloaded["max_budget_usd"] == 999.0


# ---------------------------------------------------------------------------
# from_project()
# ---------------------------------------------------------------------------


class TestFromProject:
    def test_scaffold_then_load(self, tmp_path):
        project_dir = tmp_path / "load-test"
        HarnessConfig.scaffold_project(project_dir, name="load-test")
        config = HarnessConfig.from_project(project_dir)
        assert config is not None

    def test_name_field_populated(self, tmp_path):
        project_dir = tmp_path / "my-app"
        HarnessConfig.scaffold_project(project_dir, name="my-app")
        config = HarnessConfig.from_project(project_dir)
        assert config.name == "my-app"

    def test_model_field_populated(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")
        config = HarnessConfig.from_project(project_dir)
        assert config.model is not None
        assert len(config.model) > 0

    def test_max_budget_usd_is_float(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")
        config = HarnessConfig.from_project(project_dir)
        assert isinstance(config.max_budget_usd, float)
        assert config.max_budget_usd > 0

    def test_tool_lists_are_populated(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")
        config = HarnessConfig.from_project(project_dir)
        assert isinstance(config.generator_tools, list)
        assert isinstance(config.evaluator_tools, list)
        assert isinstance(config.planner_tools, list)
        assert len(config.generator_tools) > 0

    def test_prompt_fields_populated(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")
        config = HarnessConfig.from_project(project_dir)
        assert len(config.planner_prompt.strip()) > 0
        assert len(config.generator_prompt.strip()) > 0
        assert len(config.evaluator_prompt.strip()) > 0

    def test_loads_custom_prompts(self, tmp_path):
        project_dir = tmp_path / "custom-prompts"
        HarnessConfig.scaffold_project(project_dir, name="custom-prompts")

        custom_text = "You are a custom planner. Do great things."
        (project_dir / "prompts" / "planner.md").write_text(custom_text)

        config = HarnessConfig.from_project(project_dir)
        assert config.planner_prompt == custom_text

    def test_session_mode_defaults_to_long_lived(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")
        config = HarnessConfig.from_project(project_dir)
        assert config.session_mode in ("long_lived", "fresh")

    def test_target_repo_defaults_to_none(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")
        config = HarnessConfig.from_project(project_dir)
        assert config.target_repo is None

    def test_target_repo_loaded_when_set(self, tmp_path):
        project_dir = tmp_path / "proj"
        HarnessConfig.scaffold_project(project_dir, name="proj")

        config_path = project_dir / "config.json"
        data = json.loads(config_path.read_text())
        data["target_repo"] = "/some/repo"
        config_path.write_text(json.dumps(data))

        config = HarnessConfig.from_project(project_dir)
        assert config.target_repo == "/some/repo"

    def test_missing_config_json_raises_file_not_found(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            HarnessConfig.from_project(empty_dir)

    def test_missing_config_json_error_message(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="config.json"):
            HarnessConfig.from_project(empty_dir)

    def test_nonexistent_dir_raises(self, tmp_path):
        nonexistent = tmp_path / "does-not-exist"
        with pytest.raises(FileNotFoundError):
            HarnessConfig.from_project(nonexistent)

    def test_accepts_string_path(self, tmp_path):
        project_dir = tmp_path / "string-path"
        HarnessConfig.scaffold_project(project_dir, name="string-path")
        # Pass as string instead of Path
        config = HarnessConfig.from_project(str(project_dir))
        assert config.name == "string-path"
