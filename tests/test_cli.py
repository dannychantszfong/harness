"""CLI smoke tests using Click's CliRunner.

These would have caught:
  • NameError: _require_anthropic_key (function got renamed, call site stale)
  • Planner ran before runner was selected (--claude-code → still demanded API key)
  • Resume command not registered
  • Resume failing on a project with bare-list features.json

Click's CliRunner imports the command module — so even adding a missing
import or referencing an undefined symbol in cli.py fails these tests
at collection time. That's a meaningful guard against the kind of
"didn't run the full code path" regressions we hit today.
"""

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── `harness runners` (zero-arg, no I/O) ─────────────────────────────────────

def test_runners_command_lists_all(runner):
    result = runner.invoke(main, ["runners"])
    assert result.exit_code == 0, result.output
    # All seven runners are listed in the menu
    for name in ["subprocess", "sdk", "codex", "anthropic", "openai", "gemini", "openrouter"]:
        assert name in result.output


# ── `harness new` flag parsing (no real run) ─────────────────────────────────

def test_new_rejects_multiple_runner_flags(runner):
    """Two named runner flags = explicit usage error."""
    result = runner.invoke(main, ["new", "--claude-code", "--codex"])
    assert result.exit_code != 0
    assert "Only one runner flag" in result.output


def test_new_api_mode_without_key_aborts_cleanly(runner, monkeypatch):
    """API runners must require ANTHROPIC_API_KEY and abort with the friendly panel."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(main, ["new", "--anthropic-api"])
    # SystemExit(1) bubbles through Click as exit_code 1
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_new_with_api_flag_on_subscription_runner_demands_key(runner, monkeypatch):
    """`--claude-code --with-api` forces api orchestration → key required."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # We supply --with-api, which forces orchestration_mode="api" even on subscription
    result = runner.invoke(main, ["new", "--claude-code", "--with-api"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


# ── `harness resume` ─────────────────────────────────────────────────────────

def test_resume_missing_config_yaml(runner, tmp_path):
    """Resume on a directory without config.yaml fails clearly."""
    result = runner.invoke(main, ["resume", str(tmp_path)])
    assert result.exit_code == 1
    assert "config.yaml" in result.output


def test_resume_missing_directory(runner):
    """Resume on a non-existent directory fails at Click validation."""
    result = runner.invoke(main, ["resume", "/this/path/does/not/exist/at/all"])
    assert result.exit_code != 0


def test_resume_loads_config_and_invokes_orchestrator(runner, tmp_path, monkeypatch):
    """Resume reads config.yaml and hands off to Orchestrator.run() — no other side effects."""
    # Build a valid project layout
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    config = {
        "project_name": "ResumeMe",
        "project_id": "abc123",
        "brief": "test",
        "output_dir": str(project_dir),
        "orchestration_mode": "runner",
        "code_runner": "subprocess",
    }
    (project_dir / "config.yaml").write_text(yaml.safe_dump(config))

    captured = {}

    class FakeOrchestrator:
        def __init__(self, cfg, runner_type=None):
            captured["config"] = cfg
            captured["runner_type"] = runner_type
        def run(self, *a, **kw):
            captured["ran"] = True

    # Patch the symbol the cli module looks up
    monkeypatch.setattr("cli.Orchestrator", FakeOrchestrator)

    result = runner.invoke(main, ["resume", str(project_dir)])
    assert result.exit_code == 0, result.output
    assert captured.get("ran") is True
    assert captured["config"].project_name == "ResumeMe"
    assert captured["config"].project_id == "abc123"


def test_resume_bare_list_features_shown_as_normalize_pending(
    runner, tmp_path, monkeypatch
):
    """A bare-list features.json must show as 'will normalize on init phase' —
    NOT as a Pydantic ValidationError dump. That was the scary-looking message
    the user saw."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    config = {
        "project_name": "Legacy",
        "project_id": "leg00000",
        "brief": "test",
        "output_dir": str(project_dir),
        "orchestration_mode": "runner",
        "code_runner": "subprocess",
    }
    (project_dir / "config.yaml").write_text(yaml.safe_dump(config))
    # Bare-list shape — what an agentic runner would have written
    bare = [{"id": f"f{i}", "name": f"F{i}", "description": "x", "priority": i}
            for i in range(120)]
    (project_dir / "features.json").write_text(json.dumps(bare))

    # Stub orchestrator so we only care about the pre-orchestrator print
    captured = {}
    class FakeOrchestrator:
        def __init__(self, *a, **k): pass
        def run(self, *a, **k): captured["ran"] = True
    monkeypatch.setattr("cli.Orchestrator", FakeOrchestrator)

    result = runner.invoke(main, ["resume", str(project_dir)])
    assert result.exit_code == 0, result.output
    # No Pydantic error dump
    assert "validation error" not in result.output.lower()
    assert "ProjectProgress" not in result.output
    # Friendly informative message
    assert "120 features" in result.output
    assert "normalize" in result.output.lower()


def test_resume_skips_re_init_when_features_already_present(runner, tmp_path, monkeypatch):
    """A resumed project with canonical features.json + spec.md must not re-decompose.

    This pins the orchestrator's idempotency guarantee that resume relies on.
    """
    from harness.config import HarnessConfig
    from harness.progress.models import Feature, ProjectProgress
    from harness.progress.tracker import ProgressTracker

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    config = HarnessConfig(
        project_name="ResumeMe",
        brief="b",
        output_dir=str(project_dir),
        orchestration_mode="runner",
        code_runner="subprocess",
    )
    config.save_yaml(project_dir / "config.yaml")

    # Pre-populate features.json + spec.md as if a prior session ran
    progress = ProjectProgress(
        project_name="ResumeMe", brief="b",
        features=[Feature(id="f1", name="X", description="x", priority=0)],
        spec=None,  # not yet set in progress, but spec.md exists on disk
    )
    ProgressTracker(config).save(progress)
    (project_dir / "spec.md").write_text("# Spec\nbody")

    # Capture which orchestrator phases are actually entered
    calls = []
    from harness.orchestrator import Orchestrator

    real_init = Orchestrator._initialize
    real_plan = Orchestrator._plan
    real_loop = Orchestrator._feature_loop

    def fake_init(self):
        calls.append("init")
        return real_init(self)
    def fake_plan(self, progress, confirmed_spec=None):
        calls.append("plan")
        return real_plan(self, progress, confirmed_spec)
    def fake_loop(self, progress):
        calls.append("loop")
        return  # don't actually run the loop
    def fake_runner_status(self):
        return  # skip the rich preflight banner

    monkeypatch.setattr(Orchestrator, "_initialize", fake_init)
    monkeypatch.setattr(Orchestrator, "_plan", fake_plan)
    monkeypatch.setattr(Orchestrator, "_feature_loop", fake_loop)
    monkeypatch.setattr(Orchestrator, "_print_runner_status", fake_runner_status)

    # Stub the runner factory so we don't actually construct a real one
    from unittest.mock import MagicMock
    fake_runner = MagicMock()
    fake_runner.preflight.return_value = MagicMock(ok=True, summary="", details="")
    monkeypatch.setattr("harness.orchestrator.create_runner", lambda *a, **k: fake_runner)

    result = runner.invoke(main, ["resume", str(project_dir)])
    assert result.exit_code == 0, result.output

    # All three phases were invoked, but each must have been a no-op short-circuit:
    #   init: features.json exists → tracker.load(), no decomposition
    #   plan: spec.md on disk → loaded into progress.spec, no planner agent call
    #   loop: stubbed
    assert calls == ["init", "plan", "loop"]
    # spec was promoted from spec.md
    final = ProgressTracker(config).load()
    assert final.spec is not None
    assert "Spec" in final.spec
