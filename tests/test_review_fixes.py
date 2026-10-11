"""Regressions for issues found in the Harness 1.0 review pass."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from harness.config import CONFIG_FILENAME, HarnessConfig, RunnerProfile
from harness.orchestrator import Orchestrator
from harness.progress.models import EvaluationResult, Feature, FeatureStatus, ProjectProgress
from harness.progress.tracker import ProgressTracker
from harness.project_git import git_head

pytestmark = pytest.mark.usefixtures("runners_ready")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout.strip()


def _eval(score, passed, iteration=1, reason=None, parse_failed=False):
    return EvaluationResult(design_quality=score, originality=score, craft=score, functionality=score,
                            overall_score=score, feedback=f"fb {score}", passed=passed, iteration=iteration,
                            failure_reason=reason, parse_failed=parse_failed)


def _project(tmp_path, features, *, git=True, current=None, **cfg):
    project = tmp_path / "proj"
    project.mkdir()
    config = HarnessConfig(project_name="p", brief="b", output_dir=str(project), orchestration_mode="runner",
                           sprint_contract_enabled=False, context_reset_threshold_tokens=10**9, **cfg)
    config.save_file(project / CONFIG_FILENAME)
    ProgressTracker(config).save(ProjectProgress(project_name="p", brief="b", spec="s", features=features,
                                                 current_feature_id=current))
    if git:
        _git(project, "init", "-q")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "-m", "init")
    return config


def _run_loop(config, generate, evaluations):
    orch = Orchestrator(config)
    progress = ProgressTracker(config).load()
    with (
        patch.object(orch, "_initialize", return_value=progress),
        patch.object(orch, "_plan", return_value=progress),
        patch("harness.orchestrator.GeneratorAgent") as MockGen,
        patch("harness.orchestrator.EvaluatorAgent") as MockEval,
    ):
        MockGen.return_value.implement_feature.side_effect = generate
        MockGen.return_value.usage = MagicMock(total_tokens=0, cost_usd=0.0)
        ev = MockEval.return_value
        ev.evaluate.side_effect = evaluations
        ev.usage = MagicMock(total_tokens=0, cost_usd=0.0)
        ev.last_raw_output = ""
        orch.run()
    return MockGen.return_value, ev


# ── 1. a revert must not let ignored leftovers into git ─────────────────────

@needs_git
def test_revert_keeps_attempt_ignored_files_out_of_git(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a"),
                                 Feature(id="f2", name="B", description="b", priority=1)],
                      max_iterations_per_feature=1)
    project = Path(config.output_dir)

    def generate(feature, **kw):
        if feature.id == "f1":
            (project / ".gitignore").write_text(".env\nnode_modules/\n")
            (project / ".env").write_text("API_KEY=sk-live-secret\n")
            (project / "node_modules").mkdir(exist_ok=True)
            (project / "node_modules" / "x.js").write_text("x")
            (project / "broken.py").write_text("broken")
        else:
            (project / "ok.py").write_text("ok")
        return "done"

    _run_loop(config, generate, [_eval(3, False), _eval(9, True)])

    tracked = _git(project, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".env" not in tracked and not any(p.startswith("node_modules") for p in tracked)
    assert "ok.py" in tracked and "broken.py" not in tracked
    assert (project / ".env").exists()  # nothing deleted
    assert ".env" not in _git(project, "status", "--porcelain")
    # The base .gitignore regained Harness's own entries
    assert ".auto_resume.sh" in (project / ".gitignore").read_text()


# ── 2. a stale base must never undo another feature's commit ────────────────

@needs_git
def test_restarted_feature_gets_a_fresh_base(tmp_path):
    old_base_holder = {}
    config = _project(tmp_path, [
        Feature(id="f1", name="One", description="a"),
        Feature(id="f2", name="Two", description="b", priority=1, status=FeatureStatus.FAILING,
                evaluation_history=[_eval(4, False)]),
    ], max_iterations_per_feature=2)
    project = Path(config.output_dir)
    old_base_holder["sha"] = git_head(project)
    progress = ProgressTracker(config).load()
    progress.get_feature("f2").base_commit_sha = old_base_holder["sha"]  # recorded long ago
    ProgressTracker(config).save(progress)

    def generate(feature, **kw):
        (project / f"{feature.id}.py").write_text(feature.id)
        return "x"

    _run_loop(config, generate, [_eval(9, True), _eval(4, False, 2)])

    final = ProgressTracker(config).load()
    assert final.get_feature("f1").status == FeatureStatus.PASSING
    assert (project / "f1.py").exists()  # f1's passing work is still on main
    assert _git(project, "merge-base", "--is-ancestor", final.get_feature("f1").commit_sha, "HEAD") == ""
    assert not (project / "f2.py").exists()  # f2's own attempt was reverted


@needs_git
def test_revert_refuses_to_cross_another_features_commit(tmp_path):
    config = _project(tmp_path, [Feature(id="f2", name="Two", description="b", priority=1)],
                      max_iterations_per_feature=1)
    project = Path(config.output_dir)
    base = git_head(project)
    (project / "f1.py").write_text("f1")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "feat(f1)")
    z = git_head(project)
    progress = ProgressTracker(config).load()
    progress.features.insert(0, Feature(id="f1", name="One", description="a", status=FeatureStatus.PASSING,
                                         commit_sha=z))
    f2 = progress.get_feature("f2")
    f2.status, f2.base_commit_sha = FeatureStatus.IN_PROGRESS, base
    progress.current_feature_id = "f2"  # a direct continuation keeps the old base
    ProgressTracker(config).save(progress)

    _run_loop(config, lambda **kw: "x", [_eval(3, False)])

    assert (project / "f1.py").exists()
    assert _git(project, "merge-base", "--is-ancestor", z, "HEAD") == ""


# ── 3 / 8. api-mode tool hardening ──────────────────────────────────────────

@pytest.fixture
def tools(tmp_path):
    from harness.agents.tools import ProjectTools

    cfg = HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="api")
    if shutil.which("git"):
        _git(tmp_path, "init", "-q")
    return ProjectTools(cfg)


@pytest.mark.parametrize("command", [
    "git log -1 --format=x --output=../evil.txt",
    "git diff --no-index C:/Windows/win.ini x",
    "git diff -Oorderfile",
    "git show --ext-diff",
    "git log --exec=foo",
])
def test_git_options_that_write_or_escape_are_refused(tools, command):
    assert tools.execute("run_command", {"command": command}).startswith("ERROR: git option")


@needs_git
def test_plain_git_inspection_still_runs(tools):
    out = tools.execute("run_command", {"command": "git status --short"})
    assert out.startswith("exit code 0")


@pytest.mark.parametrize("name, tool_input", [
    ("read_file", {"path": "x.py", "offset": "x"}),
    ("list_files", {"max_depth": "deep"}),
    ("read_file", {"path": "a\x00b"}),
    ("search", "not-a-dict"),
])
def test_malformed_tool_input_is_an_error_not_a_crash(tools, tmp_path, name, tool_input):
    (tmp_path / "x.py").write_text("print(1)\n")
    assert tools.execute(name, tool_input).startswith("ERROR")


# ── 4. agents cannot loosen their own grading ───────────────────────────────

def test_agent_cannot_change_grading_settings(tmp_path):
    config = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path))
    config.save_file(tmp_path / CONFIG_FILENAME)
    tracker = ProgressTracker(config)
    tracker.save(ProjectProgress(project_name="p", brief="b"))
    path = tmp_path / CONFIG_FILENAME
    original = path.read_bytes()

    with tracker.protect_state():
        data = json.loads(path.read_text())
        data["evaluator_pass_score"] = 1.0
        data["evaluator_functionality_floor"] = 0
        path.write_text(json.dumps(data))
    assert path.read_bytes() == original

    with tracker.protect_state():  # a user's model swap mid-session is kept
        data = json.loads(path.read_text())
        data["generator_model"] = "claude-sonnet-x"
        path.write_text(json.dumps(data))
    assert json.loads(path.read_text())["generator_model"] == "claude-sonnet-x"


# ── 5. reloads keep in-memory overrides ─────────────────────────────────────

def test_reload_keeps_cli_overrides_when_file_is_rewritten(tmp_path):
    config = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path), code_runner="subprocess",
                           runner_profiles=[RunnerProfile(name="claude", runner="subprocess")])
    path = tmp_path / CONFIG_FILENAME
    config.save_file(path)
    orch = Orchestrator(config)
    orch.config.runner_profiles = []          # `resume --runner subprocess`
    orch.config.code_runner_model = "X"       # `--model X`
    orch._role_runner_cache[("generator", 0)] = object()

    path.write_bytes(path.read_bytes())       # e.g. state files restored after a revert
    future = time.time() + 10
    os.utime(path, (future, future))
    assert orch._reload_config_if_changed() == {}
    assert orch.config.runner_profiles == [] and orch.config.code_runner_model == "X"

    data = json.loads(path.read_text())
    data["runner_timeout_seconds"] = 2400     # a real edit still applies …
    path.write_text(json.dumps(data))
    os.utime(path, (future + 10, future + 10))
    changes = orch._reload_config_if_changed()
    assert "runner_timeout_seconds" in changes and orch.config.code_runner_model == "X"
    assert orch._role_runner_cache == {}      # … and profile runners are rebuilt


# ── 6. timeouts kill the whole process tree ─────────────────────────────────

def test_run_process_timeout_kills_grandchildren(tmp_path):
    from harness.runners.base import run_process

    script = tmp_path / "shim.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, str(script)], cwd=str(tmp_path), timeout=2)
    assert time.monotonic() - start < 20  # not the 60 s the grandchild sleeps


def test_run_process_returns_output(tmp_path):
    from harness.runners.base import run_process

    out = run_process([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                      cwd=str(tmp_path), input="hello", timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "HELLO"


# ── 7. SDK: no rate-limit verdict from transcript chatter ───────────────────

def test_sdk_max_turns_with_429_chatter_is_not_a_cap(tmp_path, monkeypatch):
    from harness.runners.sdk_runner import SDKRunner

    class AssistantMessage:
        content = [type("T", (), {"text": "Implemented middleware returning HTTP 429 Too Many Requests"})()]

    class ResultMessage:
        subtype, is_error, result, usage, total_cost_usd, session_id = "error_max_turns", True, None, None, 0, "s"

    async def query(*a, **k):
        yield AssistantMessage()
        yield ResultMessage()

    sdk = MagicMock()
    sdk.query = query
    monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk)
    result = SDKRunner(HarnessConfig(project_name="p", brief="b")).implement("p", cwd=str(tmp_path))
    assert result.success is False and result.rate_limited is False


# ── 9. a dead evaluator stops the run instead of burning budgets ────────────

def test_evaluator_outage_stops_without_counting(tmp_path, capsys):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")], git=False,
                      max_iterations_per_feature=2)
    outage = _eval(0, False, reason="evaluator_error", parse_failed=True)
    gen, ev = _run_loop(config, lambda **kw: "x", [outage, outage])

    assert ev.evaluate.call_count == 2 and gen.implement_feature.call_count == 1
    f1 = ProgressTracker(config).load().get_feature("f1")
    assert f1.evaluation_history == [] and f1.status == FeatureStatus.IN_PROGRESS
    assert "evaluator unavailable" in capsys.readouterr().out


# ── 10. PowerShell quoting of typographic apostrophes ───────────────────────

def test_powershell_wrapper_escapes_typographic_quotes(tmp_path):
    from harness import auto_resume

    project = tmp_path / "Danny\u2019s app"
    project.mkdir()
    text = auto_resume._powershell_wrapper(project, "harness", project / "log", "T")
    assert "Danny\u2019\u2019s app" in text
    if shutil.which("powershell"):
        ps1 = tmp_path / "check.ps1"
        ps1.write_text(text.replace("schtasks", "#").replace("& $Harness", "# &"), encoding="utf-8-sig")
        parsed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$null = [ScriptBlock]::Create((Get-Content -Raw -LiteralPath '{ps1}')); 'ok'"],
            capture_output=True, text=True,
        )
        assert parsed.stdout.strip() == "ok", parsed.stderr


# ── 11. more literal secrets ────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"project_git_remote": "https://ghp_abc123@github.com/o/r.git"},
    {"code_runner_extra_args": ["--api-key", "sk-live-1"]},
    {"code_runner_extra_args": ["--token=abc"]},
    {"runner_profiles": [{"name": "x", "runner": "codex", "extra_args": ["--api-key", "sk-1"]}]},
])
def test_more_literal_secrets_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        HarnessConfig(project_name="p", brief="b", **kwargs)


def test_ordinary_remotes_and_args_are_fine():
    HarnessConfig(project_name="p", brief="b", project_git_remote="git@github.com:o/r.git",
                  code_runner_extra_args=["--verbose", "--max-turns", "5"])
    HarnessConfig(project_name="p", brief="b", project_git_remote="https://github.com/o/r.git")


def test_cli_rejects_git_remote_with_token():
    from harness import cli

    result = CliRunner().invoke(cli.main, ["new", "--claude-code", "--git-remote", "https://tok@github.com/o/r.git"])
    assert result.exit_code == 2 and "credentials" in result.output


# ── 12. `harness run file.yaml` leaves a resumable project ──────────────────

def test_run_writes_project_config_and_keeps_project_id(tmp_path, monkeypatch):
    from harness import cli

    class FakeOrch:
        def __init__(self, *a, **k):
            pass

        def run(self, **k):
            pass

    monkeypatch.setattr(cli, "Orchestrator", FakeOrch)
    monkeypatch.setattr(cli, "load_setup", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.yaml").write_text("project_name: app\nbrief: b\noutput_dir: ./output/app\n"
                                       "code_runner: subprocess\n")

    assert CliRunner().invoke(cli.main, ["run", "app.yaml"]).exit_code == 0
    saved = tmp_path / "output" / "app" / CONFIG_FILENAME
    first_id = json.loads(saved.read_text())["project_id"]
    assert CliRunner().invoke(cli.main, ["run", "app.yaml"]).exit_code == 0
    assert json.loads(saved.read_text())["project_id"] == first_id
