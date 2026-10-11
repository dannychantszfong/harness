"""Config hygiene: secrets (P-19), profile models (P-22), timestamps (P-24),
lazy package import (P-28)."""

import json
import subprocess
import sys
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.config import HarnessConfig, RunnerProfile
from harness.progress.models import ProjectProgress
from harness.progress.tracker import ProgressTracker
from harness.project_git import PROJECT_GITIGNORE_ENTRIES, ensure_project_gitignore
from harness.runner_profiles import HarnessSetup, config_for_profile


def _cfg(tmp_path, **kw) -> HarnessConfig:
    return HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), **kw)


# ── P-19: literal secrets never reach harness_config.json ───────────────────

@pytest.mark.parametrize("field", ["openai_api_key", "gemini_api_key", "openrouter_api_key"])
def test_literal_api_key_field_is_rejected(tmp_path, field):
    with pytest.raises(ValidationError) as exc:
        _cfg(tmp_path, **{field: "sk-live-abc123"})
    assert "literal secret" in str(exc.value)
    assert "$" in str(exc.value)  # the fix hint names an env reference


@pytest.mark.parametrize("value", ["$OPENAI_API_KEY", "${OPENAI_API_KEY}"])
def test_env_reference_api_key_is_accepted(tmp_path, value):
    assert _cfg(tmp_path, openai_api_key=value).openai_api_key == value


def test_secret_looking_profile_env_must_be_reference():
    with pytest.raises(ValidationError):
        RunnerProfile(name="or", runner="subprocess", env={"ANTHROPIC_AUTH_TOKEN": "sk-or-123"})
    ok = RunnerProfile(
        name="or",
        runner="subprocess",
        env={"ANTHROPIC_AUTH_TOKEN": "$OPENROUTER_API_KEY", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1"},
    )
    assert ok.env["ANTHROPIC_BASE_URL"].startswith("https://")


def test_literal_secret_in_active_runner_env_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        _cfg(tmp_path, active_runner_env={"MY_SECRET": "hunter2"})
    assert _cfg(tmp_path, active_runner_env={"DEBUG": "1"}).active_runner_env == {"DEBUG": "1"}


def test_loading_a_config_with_a_literal_secret_fails(tmp_path):
    path = tmp_path / "harness_config.json"
    path.write_text(json.dumps({"project_name": "t", "brief": "b", "openai_api_key": "sk-abc"}))
    with pytest.raises(ValidationError):
        HarnessConfig.from_file(path)


def test_save_refuses_a_secret_assigned_after_load(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.openrouter_api_key = "sk-or-literal"  # assignment is not validated
    with pytest.raises(ValueError, match="literal secret"):
        cfg.save_file(tmp_path / "harness_config.json")
    assert not (tmp_path / "harness_config.json").exists()


def test_setup_with_literal_secret_does_not_load():
    with pytest.raises(ValidationError):
        HarnessSetup(runner_profiles=[{"name": "x", "runner": "codex", "env": {"OPENAI_API_KEY": "sk-1"}}])


def test_project_gitignore_gets_runtime_entries_once(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/")
    assert ensure_project_gitignore(tmp_path) is True
    assert ensure_project_gitignore(tmp_path) is False  # idempotent
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/"
    for entry in PROJECT_GITIGNORE_ENTRIES:
        assert lines.count(entry) == 1
    assert ".auto_resume.sh" in PROJECT_GITIGNORE_ENTRIES
    assert "auto_resume.log" in PROJECT_GITIGNORE_ENTRIES


# ── P-22: profile model inheritance only within the same runtime ────────────

def test_profile_on_other_runtime_does_not_inherit_model(tmp_path):
    base = _cfg(tmp_path, code_runner="subprocess", code_runner_model="opus")
    codex = config_for_profile(base, RunnerProfile(name="codex", runner="codex"))
    assert codex.code_runner_model is None  # never `codex --model opus`


def test_profile_on_same_runtime_inherits_model(tmp_path):
    base = _cfg(tmp_path, code_runner="subprocess", code_runner_model="opus")
    same = config_for_profile(base, RunnerProfile(name="claude", runner="subprocess"))
    assert same.code_runner_model == "opus"
    explicit = config_for_profile(base, RunnerProfile(name="c2", runner="codex", model="gpt-5.2"))
    assert explicit.code_runner_model == "gpt-5.2"


# ── P-24: tz-aware timestamps, naive legacy files still load ────────────────

def test_legacy_naive_timestamps_load_as_utc(tmp_path):
    cfg = _cfg(tmp_path)
    legacy = {
        "project_name": "t",
        "brief": "b",
        "created_at": "2026-05-01T10:00:00",
        "last_updated": "2026-05-01T10:05:00.123456",
        "features": [{
            "id": "f1", "name": "A", "description": "a",
            "last_updated": "2026-05-01T10:00:00",
            "evaluation_history": [{
                "design_quality": 5, "originality": 5, "craft": 5, "functionality": 5,
                "overall_score": 5, "feedback": "x", "passed": False, "iteration": 1,
                "evaluated_at": "2026-05-01T10:01:00",
            }],
        }],
    }
    cfg.features_path.write_text(json.dumps(legacy))
    progress = ProgressTracker(cfg).load()
    assert progress.created_at.tzinfo is not None
    assert progress.created_at.utcoffset().total_seconds() == 0
    assert progress.features[0].evaluation_history[0].evaluated_at.tzinfo is not None


def test_new_timestamps_are_tz_aware():
    progress = ProjectProgress(project_name="t", brief="b")
    assert progress.created_at.tzinfo is not None
    assert progress.created_at.astimezone(timezone.utc) == progress.created_at


def test_harness_sources_do_not_call_utcnow():
    root = Path(__file__).resolve().parents[1] / "harness"
    offenders = [p.name for p in root.rglob("*.py") if "datetime.utcnow" in p.read_text(encoding="utf-8")
                 and p.name != "timeutil.py"]
    assert offenders == []


# ── P-28: importing the config does not import the orchestrator ─────────────

def test_import_harness_config_is_lazy():
    code = (
        "import sys, harness.config; "
        "print('harness.orchestrator' in sys.modules, 'harness.agents' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], check=True)
    assert out.stdout.split() == ["False", "False"]


def test_package_level_names_still_resolve():
    import harness

    assert harness.HarnessConfig is HarnessConfig
    from harness.orchestrator import Orchestrator

    assert harness.Orchestrator is Orchestrator
