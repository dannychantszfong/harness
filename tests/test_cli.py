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

import cli
from cli import main
from harness.config import CONFIG_FILENAME
from harness.runner_profiles import HarnessSetup, save_setup
from harness.runners.base import PreflightResult, RunResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── `harness runners` (zero-arg, no I/O) ─────────────────────────────────────

def test_runners_command_lists_all(runner):
    result = runner.invoke(main, ["runners"])
    assert result.exit_code == 0, result.output
    # The three coding-agent runners — API providers no longer appear here
    for name in ["subprocess", "sdk", "codex"]:
        assert name in result.output
    for legacy in ["anthropic", "openai", "gemini", "openrouter"]:
        assert legacy not in result.output


def test_new_no_longer_accepts_api_runner_flags(runner):
    """The four API-runner flags must be removed entirely."""
    for flag in ["--anthropic-api", "--openai-api", "--gemini", "--openrouter"]:
        result = runner.invoke(main, ["new", flag])
        # Click emits a "no such option" usage error — exit code 2
        assert result.exit_code != 0
        assert "no such option" in result.output.lower() or "not a valid" in result.output.lower()


# ── `harness new` flag parsing (no real run) ─────────────────────────────────

def test_new_rejects_multiple_runner_flags(runner):
    """Two named runner flags = explicit usage error."""
    result = runner.invoke(main, ["new", "--claude-code", "--codex"])
    assert result.exit_code != 0
    assert "Only one runner flag" in result.output


def test_new_with_api_flag_on_subscription_runner_demands_key(runner, monkeypatch):
    """`--claude-code --with-api` forces api orchestration → key required."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # We supply --with-api, which forces orchestration_mode="api" even on subscription
    result = runner.invoke(main, ["new", "--claude-code", "--with-api"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_new_persists_agentic_coding_model(runner, tmp_path, monkeypatch):
    """`harness new --codex --model ...` stores the model before project start."""
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    fake_runner = MagicMock()
    monkeypatch.setattr("harness.runners.create_runner", lambda *a, **k: fake_runner)
    monkeypatch.setattr(
        "harness.agents.planner.PlannerAgent.align_requirements",
        lambda self, brief: "# Confirmed spec",
    )

    captured = {}

    class FakeOrchestrator:
        def __init__(self, cfg, runner_type=None):
            captured["config"] = cfg
            captured["runner_type"] = runner_type
        def run(self, *a, **kw):
            captured["ran"] = True

    monkeypatch.setattr("cli.Orchestrator", FakeOrchestrator)

    result = runner.invoke(
        main,
        ["new", "--codex", "--model", "gpt-5.2"],
        input="Harness\nBuild an agent harness\n",
    )
    assert result.exit_code == 0, result.output
    assert captured["ran"] is True
    assert captured["config"].code_runner == "codex"
    assert captured["config"].code_runner_model == "gpt-5.2"

    config_files = list((tmp_path / "output").glob(f"*/{CONFIG_FILENAME}"))
    assert len(config_files) == 1
    saved = yaml.safe_load(config_files[0].read_text())
    assert saved["code_runner_model"] == "gpt-5.2"


def test_new_persists_project_github_repo(runner, tmp_path, monkeypatch):
    """Generated projects can be configured to push to their own GitHub repo."""
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    fake_runner = MagicMock()
    monkeypatch.setattr("harness.runners.create_runner", lambda *a, **k: fake_runner)
    monkeypatch.setattr(
        "harness.agents.planner.PlannerAgent.align_requirements",
        lambda self, brief: "# Confirmed spec",
    )

    captured = {}

    class FakeOrchestrator:
        def __init__(self, cfg, runner_type=None):
            captured["config"] = cfg
        def run(self, *a, **kw):
            captured["ran"] = True

    monkeypatch.setattr("cli.Orchestrator", FakeOrchestrator)

    result = runner.invoke(
        main,
        [
            "new",
            "--claude-code",
            "--model",
            "sonnet",
            "--github-repo",
            "danny/my-app",
            "--github-public",
        ],
        input="Harness\nBuild a thing\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["config"].project_git_push is True
    assert captured["config"].project_github_repo == "danny/my-app"
    assert captured["config"].project_github_private is False


def test_setup_writes_runner_rotation_config(runner, tmp_path):
    setup_path = tmp_path / "setup.json"

    result = runner.invoke(main, [
        "setup",
        "--config", str(setup_path),
        "--profile", "claude:subprocess:sonnet",
        "--profile", "codex:codex:gpt-5.2",
        "--profile", "openrouter:subprocess:anthropic/claude-sonnet-4-6:openrouter",
        "--profile-env", "openrouter:ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1",
        "--profile-env", "openrouter:ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY",
        "--planner-order", "codex,claude",
        "--generator-order", "claude,codex,openrouter",
        "--evaluator-order", "codex,openrouter",
    ])

    assert result.exit_code == 0, result.output
    data = json.loads(setup_path.read_text())
    assert data["generator_runner_order"] == ["claude", "codex", "openrouter"]
    openrouter = next(p for p in data["runner_profiles"] if p["name"] == "openrouter")
    assert openrouter["provider"] == "openrouter"
    assert openrouter["env"]["ANTHROPIC_AUTH_TOKEN"] == "$OPENROUTER_API_KEY"


def test_new_loads_setup_profiles_without_runner_prompt(runner, tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    setup_path = tmp_path / "setup.json"
    save_setup(
        HarnessSetup(
            runner_profiles=[
                {
                    "name": "claude",
                    "runner": "subprocess",
                    "model": "sonnet",
                },
                {
                    "name": "codex",
                    "runner": "codex",
                    "model": "gpt-5.2",
                },
            ],
            planner_runner_order=["codex"],
            generator_runner_order=["claude", "codex"],
            evaluator_runner_order=["codex"],
            reviewer_runner_order=["codex"],
        ),
        setup_path,
    )
    monkeypatch.setenv("HARNESS_SETUP_CONFIG", str(setup_path))
    monkeypatch.chdir(tmp_path)
    fake_runner = MagicMock()
    monkeypatch.setattr("harness.runners.create_runner", lambda *a, **k: fake_runner)
    monkeypatch.setattr("harness.runner_profiles.create_runner", lambda *a, **k: fake_runner)
    monkeypatch.setattr(
        "harness.agents.planner.PlannerAgent.align_requirements",
        lambda self, brief: "# Confirmed spec",
    )

    captured = {}

    class FakeOrchestrator:
        def __init__(self, cfg, runner_type=None):
            captured["config"] = cfg
            captured["runner_type"] = runner_type
        def run(self, *a, **kw):
            captured["ran"] = True

    monkeypatch.setattr("cli.Orchestrator", FakeOrchestrator)

    result = runner.invoke(main, ["new"], input="Harness\nBuild a thing\n")

    assert result.exit_code == 0, result.output
    assert captured["ran"] is True
    assert captured["runner_type"].value == "subprocess"
    assert captured["config"].orchestration_mode == "runner"
    assert captured["config"].generator_runner_order == ["claude", "codex"]
    assert captured["config"].planner_runner_order == ["codex"]


# ── `harness animation-theme` ────────────────────────────────────────────────

def test_animation_theme_invokes_agentic_runner(runner, tmp_path, monkeypatch):
    """Theme customization should hand a precise patch task to a signed-in agent."""
    root = tmp_path
    (root / "harness" / "ui").mkdir(parents=True)
    (root / "harness" / "ui" / "spinner.py").write_text("PHRASES = {}\n")
    (root / "docs" / "technical").mkdir(parents=True)
    (root / "docs" / "technical" / "animation_theme_agent_guide.md").write_text(
        "Guide marker: edit PHRASES[\"playful\"]."
    )
    monkeypatch.setattr(cli, "_harness_source_root", lambda: root)

    captured = {}

    class FakeRunner:
        def preflight(self):
            return PreflightResult(ok=True, summary="ready", details="fake")

        def implement(self, prompt, cwd, timeout_seconds=600):
            captured["prompt"] = prompt
            captured["cwd"] = cwd
            captured["timeout"] = timeout_seconds
            return RunResult(output="changed harness/ui/spinner.py", success=True)

    def fake_create_runner(runner_type, config):
        captured["runner_type"] = runner_type
        captured["config"] = config
        return FakeRunner()

    monkeypatch.setattr("harness.runners.create_runner", fake_create_runner)

    result = runner.invoke(
        main,
        [
            "animation-theme",
            "frost",
            "library",
            "--runner",
            "codex",
            "--model",
            "gpt-test",
            "--timeout",
            "123",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["runner_type"].value == "codex"
    assert captured["config"].code_runner == "codex"
    assert captured["config"].code_runner_model == "gpt-test"
    assert captured["cwd"] == str(root)
    assert captured["timeout"] == 123
    assert "frost library" in captured["prompt"]
    assert "harness/ui/spinner.py" in captured["prompt"]
    assert 'PHRASES["playful"]' in captured["prompt"]
    assert "single Title Case verb" in captured["prompt"]
    assert "Guide marker" in captured["prompt"]
    assert "Animation theme agent finished" in result.output


def test_animation_theme_rejects_api_runner(runner):
    """API runners are text-only, so the command only accepts agentic runners."""
    result = runner.invoke(main, ["animation-theme", "moon", "--runner", "openai"])
    assert result.exit_code != 0
    assert "invalid value" in result.output.lower()
    assert "subprocess" in result.output
    assert "codex" in result.output


# ── `harness resume` ─────────────────────────────────────────────────────────

def test_resume_missing_config_file(runner, tmp_path):
    """Resume on a directory without harness_config.json fails clearly."""
    result = runner.invoke(main, ["resume", str(tmp_path)])
    assert result.exit_code == 1
    assert CONFIG_FILENAME in result.output


def test_resume_missing_directory(runner):
    """Resume on a non-existent directory fails at Click validation."""
    result = runner.invoke(main, ["resume", "/this/path/does/not/exist/at/all"])
    assert result.exit_code != 0


def test_resume_loads_config_and_invokes_orchestrator(runner, tmp_path, monkeypatch):
    """Resume reads harness_config.json and hands off to Orchestrator.run()."""
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
    (project_dir / CONFIG_FILENAME).write_text(json.dumps(config))

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
    (project_dir / CONFIG_FILENAME).write_text(json.dumps(config))
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
    config.save_yaml(project_dir / CONFIG_FILENAME)

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

    def fake_init(self, *a, **k):
        calls.append("init")
        return real_init(self, *a, **k)
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
