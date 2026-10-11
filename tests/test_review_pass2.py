"""Regressions for docs/HARNESS_1.0_REVIEW_PASS_2.md (R-01 … R-07, N-01)."""

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from harness import auto_resume, process_guard, run_lock
from harness.config import CONFIG_FILENAME, HarnessConfig
from harness.orchestrator import Orchestrator
from harness.progress.models import Feature, FeatureStatus
from harness.progress.tracker import ProgressTracker
from harness.project_git import ensure_project_gitignore, nested_repos
from harness.runners.base import RunnerFailedError, RunnerTurnLimitError

from tests.test_review_fixes import _eval, _git, _project, _run_loop

pytestmark = pytest.mark.usefixtures("runners_ready")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
REPO = Path(__file__).resolve().parents[1]


def _commit_everything(project: Path) -> None:
    ensure_project_gitignore(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "setup")


def _reject_commits(project: Path, tmp_path: Path, count: int) -> None:
    """A pre-commit hook (husky/lint-staged style) rejecting the next `count` commits."""
    counter = (tmp_path / "hook_count").as_posix()
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        n=$(cat "{counter}" 2>/dev/null || echo 0)
        echo $((n+1)) > "{counter}"
        if [ "$n" -lt {count} ]; then echo "lint-staged: rejected" >&2; exit 1; fi
        exit 0
        """), newline="\n")
    hook.chmod(0o755)


def _tracked(project: Path) -> list[str]:
    return _git(project, "ls-tree", "-r", "--name-only", "HEAD").splitlines()


# ── R-01: an uncommitted passing feature is never reset away ────────────────

@needs_git
def test_passing_feature_with_rejected_commit_is_committed_before_the_next_feature(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="One", description="a"),
                                 Feature(id="f2", name="Two", description="b", priority=1)],
                      max_iterations_per_feature=1)
    project = Path(config.output_dir)
    _commit_everything(project)
    _reject_commits(project, tmp_path, count=1)  # only f1's own commit

    def generate(feature, **kw):
        (project / f"{feature.id}.py").write_text(feature.id)
        return "done"

    _run_loop(config, generate, [_eval(9, True), _eval(3, False)])

    final = ProgressTracker(config).load()
    f1 = final.get_feature("f1")
    assert f1.status == FeatureStatus.PASSING and f1.commit_sha
    assert (project / "f1.py").exists() and "f1.py" in _tracked(project)
    assert _git(project, "merge-base", "--is-ancestor", f1.commit_sha, "HEAD") == ""
    assert not (project / "f2.py").exists()  # f2's own attempt was still reverted
    assert "passing work committed by Harness before f2" in _git(project, "log", "--format=%s")


@needs_git
def test_passing_feature_survives_when_every_commit_is_rejected(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="One", description="a"),
                                 Feature(id="f2", name="Two", description="b", priority=1)],
                      max_iterations_per_feature=1)
    project = Path(config.output_dir)
    _commit_everything(project)
    _reject_commits(project, tmp_path, count=999)

    def generate(feature, **kw):
        (project / f"{feature.id}.py").write_text(feature.id)
        return "done"

    _run_loop(config, generate, [_eval(9, True), _eval(3, False)])

    final = ProgressTracker(config).load()
    assert final.get_feature("f1").status == FeatureStatus.PASSING
    assert (project / "f1.py").read_text() == "f1"  # never reset away
    assert (project / "f2.py").exists()  # no base could be recorded → nothing reverted


@needs_git
def test_passing_feature_killed_before_its_commit_is_reconciled(tmp_path):
    """Harness died between record_evaluation(passed) and its commit."""
    config = _project(tmp_path, [
        Feature(id="f1", name="One", description="a", status=FeatureStatus.PASSING,
                evaluation_history=[_eval(9, True)]),
        Feature(id="f2", name="Two", description="b", priority=1),
    ], max_iterations_per_feature=1)
    project = Path(config.output_dir)
    _commit_everything(project)
    (project / "f1.py").write_text("f1")  # f1's work, never committed

    def generate(feature, **kw):
        (project / "f2.py").write_text("f2")
        return "done"

    _run_loop(config, generate, [_eval(3, False)])

    f1 = ProgressTracker(config).load().get_feature("f1")
    assert (project / "f1.py").exists() and not (project / "f2.py").exists()
    assert f1.commit_sha and "f1.py" in _git(project, "ls-tree", "-r", "--name-only", f1.commit_sha)


@needs_git
def test_only_state_file_changes_need_no_snapshot_commit(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="One", description="a")])
    project = Path(config.output_dir)
    _commit_everything(project)
    head = _git(project, "rev-parse", "HEAD")
    progress = ProgressTracker(config).load()
    progress.session_count = 3
    ProgressTracker(config).save(progress)  # features.json / progress.md dirty

    orch = Orchestrator(config)
    progress, base = orch._fresh_base(ProgressTracker(config).load(), "f1")
    assert base == head  # no "chore(harness): snapshot" commit for state files


def test_revert_blocker_protects_a_passing_feature_without_commit(tmp_path):
    config = _project(tmp_path, [
        Feature(id="f1", name="One", description="a", status=FeatureStatus.PASSING),
        Feature(id="f2", name="Two", description="b", priority=1),
    ], git=False)
    progress = ProgressTracker(config).load()
    reason = Orchestrator(config)._revert_blocker(progress, "f2", "base", "head")
    assert reason and "f1" in reason


# ── R-02: agent sessions die with Harness ───────────────────────────────────

_PARENT = """\
import sys
sys.path.insert(0, {repo!r})
from harness import process_guard
from harness.runners.base import run_process
process_guard.install()
run_process([sys.executable, {child!r}, {pidfile!r}], cwd={cwd!r}, timeout=120)
"""

_CHILD = """\
import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as fh:
    fh.write(f"{os.getpid()} {grandchild.pid}")
os.replace(tmp, sys.argv[1])
time.sleep(120)
"""


def _start_fake_harness(tmp_path):
    child = tmp_path / "agent.py"
    child.write_text(_CHILD)
    pidfile = tmp_path / "pids"
    parent = tmp_path / "harness_parent.py"
    parent.write_text(_PARENT.format(repo=str(REPO), child=str(child), pidfile=str(pidfile), cwd=str(tmp_path)))
    proc = subprocess.Popen([sys.executable, str(parent)])
    deadline = time.monotonic() + 30
    while not pidfile.exists():
        assert time.monotonic() < deadline, "fake agent never started"
        assert proc.poll() is None, "fake harness exited early"
        time.sleep(0.1)
    agent, grandchild = (int(x) for x in pidfile.read_text().split())
    return proc, agent, grandchild


def _kill(pid: int) -> None:
    if not process_guard.pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


def _wait_dead(*pids, timeout=15.0) -> list[int]:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if process_guard.pid_alive(p)]
        time.sleep(0.1)
    return alive


def test_agent_tree_dies_when_harness_is_terminated(tmp_path):
    """SIGTERM on POSIX (terminal closed, `kill`); TerminateProcess on Windows,
    which no handler can intercept — the kill-on-close job does the work."""
    proc, agent, grandchild = _start_fake_harness(tmp_path)
    try:
        proc.terminate()
        proc.wait(timeout=20)
        assert _wait_dead(agent, grandchild) == []
        if os.name != "nt":
            assert proc.returncode == 128 + signal.SIGTERM  # clean SystemExit path
    finally:
        for pid in (agent, grandchild):
            _kill(pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_agent_dies_when_harness_is_sigkilled_on_linux(tmp_path):
    proc, agent, grandchild = _start_fake_harness(tmp_path)
    try:
        proc.kill()
        proc.wait(timeout=20)
        assert _wait_dead(agent) == []
    finally:
        for pid in (agent, grandchild):
            _kill(pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_guard_keeps_an_ignored_sighup_ignored():
    code = (
        f"import signal, sys; sys.path.insert(0, {str(REPO)!r})\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "from harness import process_guard; process_guard.install()\n"
        "assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN  # nohup harness …\n"
        "assert signal.getsignal(signal.SIGTERM) is process_guard._exit_on_signal\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_agent_commands_install_the_guard(monkeypatch):
    from harness import cli

    calls = []
    monkeypatch.setattr(process_guard, "install", lambda: calls.append(1))
    CliRunner().invoke(cli.main, ["resume", "--help"])
    CliRunner().invoke(cli.main, ["status", "--help"])
    assert calls == [1]


# ── R-03: one Harness per project; a manual resume cancels the scheduled one ─

_HOLDER = """\
import sys, time
sys.path.insert(0, {repo!r})
from harness import run_lock
run_lock.claim({project!r}, command="harness resume")
print("held", flush=True)
time.sleep(120)
"""


def _hold_in_other_process(tmp_path, project):
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER.format(repo=str(REPO), project=str(project)))
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_second_process_is_refused_and_lock_frees_when_holder_dies(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    holder = _hold_in_other_process(tmp_path, project)
    try:
        with pytest.raises(run_lock.ProjectLockedError) as exc:
            run_lock.claim(project)
        assert str(holder.pid) in str(exc.value) and "harness resume" in str(exc.value)
    finally:
        holder.kill()  # no clean release: the OS frees the lock
        holder.wait(timeout=20)
    with run_lock.hold(project):
        assert run_lock.is_held_here(project)
    assert not run_lock.is_held_here(project)


def test_hold_is_reentrant_within_a_process(tmp_path):
    with run_lock.hold(tmp_path):
        with run_lock.hold(tmp_path):
            pass
        assert run_lock.is_held_here(tmp_path)  # the inner block did not release it
    assert not run_lock.is_held_here(tmp_path)


def test_resume_refuses_while_another_run_holds_the_project(tmp_path):
    from harness import cli

    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")], git=False)
    project = Path(config.output_dir)
    before = (project / CONFIG_FILENAME).read_bytes()
    holder = _hold_in_other_process(tmp_path, project)
    try:
        with patch("harness.cli.Orchestrator") as orch:
            result = CliRunner().invoke(cli.main, ["resume", str(project)])
        assert result.exit_code == 1 and "already working" in result.output
        orch.assert_not_called()
        assert (project / CONFIG_FILENAME).read_bytes() == before
    finally:
        holder.kill()
        holder.wait(timeout=20)
        run_lock.release(project)


def test_run_cancels_a_pending_auto_resume(tmp_path, monkeypatch):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a", status=FeatureStatus.PASSING)],
                      git=False)
    project = Path(config.output_dir)
    (project / ".auto_resume.ps1").write_text("# scheduled")
    cancelled = []
    monkeypatch.setattr(auto_resume, "cancel", lambda project_id: cancelled.append(project_id))
    monkeypatch.delenv(auto_resume.JOB_ENV_VAR, raising=False)

    _run_loop(config, lambda **kw: "x", [])

    assert cancelled == [config.project_id]
    assert not (project / ".auto_resume.ps1").exists()


def test_the_scheduled_job_does_not_cancel_itself(tmp_path, monkeypatch):
    (tmp_path / ".auto_resume.sh").write_text("#!/bin/sh")
    monkeypatch.setenv(auto_resume.JOB_ENV_VAR, "1")
    monkeypatch.setattr(auto_resume, "cancel", lambda project_id: pytest.fail("cancelled itself"))
    assert auto_resume.cancel_pending(tmp_path, "pid") is False
    assert (tmp_path / ".auto_resume.sh").exists()


def test_wrappers_mark_the_resumed_run_as_the_job(tmp_path):
    sh = auto_resume._sh_wrapper(tmp_path, "harness", tmp_path / "log")
    ps = auto_resume._powershell_wrapper(tmp_path, "harness", tmp_path / "log", "task")
    assert f"export {auto_resume.JOB_ENV_VAR}=1" in sh
    assert f"$env:{auto_resume.JOB_ENV_VAR} = '1'" in ps


@needs_git
def test_lock_file_is_neither_committed_nor_snapshotted(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a"),
                                 Feature(id="f2", name="B", description="b", priority=1)],
                      max_iterations_per_feature=1)
    project = Path(config.output_dir)

    def generate(feature, **kw):
        (project / f"{feature.id}.py").write_text("x")
        return "x"

    _run_loop(config, generate, [_eval(9, True), _eval(3, False)])

    assert (project / run_lock.LOCK_RELPATH).exists()
    assert run_lock.LOCK_RELPATH not in _git(project, "ls-files")
    assert run_lock.lock_path(project) not in Orchestrator(config)._state_files()


# ── R-04: nested repositories become plain directories ──────────────────────

def _scaffold_nested_repo(project: Path) -> None:
    app = project / "app"
    app.mkdir()
    (app / "main.rb").write_text("puts 1\n")
    _git(app, "init", "-q")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "rails new")


@needs_git
def test_nested_repo_is_committed_as_files_not_a_gitlink(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")])
    project = Path(config.output_dir)

    def generate(feature, **kw):
        _scaffold_nested_repo(project)
        return "x"

    _run_loop(config, generate, [_eval(9, True)])

    stage = _git(project, "ls-files", "--stage", "app")
    assert "app/main.rb" in stage and not stage.startswith("160000")
    assert not (project / "app" / ".git").exists()
    assert (project / ".git" / "harness" / "nested-repos" / "app" / "HEAD").exists()  # history kept


@needs_git
def test_nested_repo_from_an_exhausted_attempt_is_removed_by_the_reset(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a"),
                                 Feature(id="f2", name="B", description="b", priority=1)],
                      max_iterations_per_feature=1)
    project = Path(config.output_dir)

    def generate(feature, **kw):
        if feature.id == "f1":
            _scaffold_nested_repo(project)
        else:
            (project / "b.py").write_text("b")
        return "x"

    _run_loop(config, generate, [_eval(3, False), _eval(9, True)])

    assert not (project / "app").exists()
    assert "160000" not in _git(project, "ls-files", "--stage")
    assert "app/main.rb" in _git(project, "ls-tree", "-r", "--name-only", "harness/attempt/f1/1")


@needs_git
def test_declared_submodules_are_left_alone(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "vendor").mkdir()
    _git(tmp_path / "vendor", "init", "-q")
    (tmp_path / "loose").mkdir()
    _git(tmp_path / "loose", "init", "-q")
    (tmp_path / ".gitmodules").write_text('[submodule "vendor"]\n\tpath = vendor\n\turl = ../vendor\n')
    assert nested_repos(tmp_path) == ["loose"]


# ── R-05: a broken generator runner stops the run without counting ──────────

def test_generator_runner_outage_stops_without_counting(tmp_path, capsys):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")], git=False,
                      max_iterations_per_feature=3)

    def generate(**kw):
        raise RunnerFailedError("`claude` binary not found on PATH")

    gen, ev = _run_loop(config, generate, [])

    assert gen.implement_feature.call_count == 2 and ev.evaluate.call_count == 0
    f1 = ProgressTracker(config).load().get_feature("f1")
    assert f1.evaluation_history == [] and f1.status == FeatureStatus.IN_PROGRESS
    out = capsys.readouterr().out
    assert "generator unavailable" in out and "binary not found" in out


def test_one_generator_runner_failure_is_retried(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")], git=False)
    calls = []

    def generate(**kw):
        calls.append(kw["iteration"])
        if len(calls) == 1:
            raise RunnerFailedError("connection reset")
        return "done"

    _run_loop(config, generate, [_eval(9, True)])

    assert calls == [1, 1]  # same iteration, not counted twice
    f1 = ProgressTracker(config).load().get_feature("f1")
    assert f1.status == FeatureStatus.PASSING and f1.iteration_count == 1


def test_turn_limit_is_a_counted_failed_iteration(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")], git=False,
                      max_iterations_per_feature=2)
    outcomes = iter([RunnerTurnLimitError("session ended with error_max_turns"), "done"])

    def generate(**kw):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    _run_loop(config, generate, [_eval(9, True, iteration=2)])

    history = ProgressTracker(config).load().get_feature("f1").evaluation_history
    assert history[0].failure_reason == "generator_turn_limit" and "turn limit" in history[0].feedback
    assert history[1].passed


def test_generator_raises_for_runner_errors_and_turn_limits(tmp_path):
    from unittest.mock import MagicMock

    from harness.agents.generator import GeneratorAgent
    from harness.runners.base import RunResult

    cfg = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path), orchestration_mode="runner")
    runner = MagicMock()
    feature = Feature(id="f1", name="A", description="a")
    runner.implement.return_value = RunResult(output="", success=False, error="Not logged in")
    with pytest.raises(RunnerFailedError, match="Not logged in"):
        GeneratorAgent(cfg, runner=runner).implement_feature(feature=feature, progress=MagicMock(), session_preamble="")
    runner.implement.return_value = RunResult(output="", success=False, error="max", turn_limited=True)
    with pytest.raises(RunnerTurnLimitError):
        GeneratorAgent(cfg, runner=runner).implement_feature(feature=feature, progress=MagicMock(), session_preamble="")


# ── R-06: api-mode tools stay inside the project ────────────────────────────

@pytest.fixture
def tools(tmp_path):
    from harness.agents.tools import ProjectTools

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("SECRET_IN_PROJECT = 1\n")
    return ProjectTools(HarnessConfig(project_name="p", brief="b", output_dir=str(project)))


def test_search_does_not_follow_symlinks_out_of_the_project(tools, tmp_path):
    (tmp_path / "outside.txt").write_text("SECRET_OUTSIDE = 1\n")
    try:
        os.symlink(tmp_path / "outside.txt", tools.root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    hits = tools.search("SECRET")
    assert "SECRET_IN_PROJECT" in hits and "SECRET_OUTSIDE" not in hits
    assert "outside the project" in tools.execute("read_file", {"path": "link.txt"})


@pytest.mark.parametrize("command", [
    "git diff -pO/etc/passwd",      # short-option bundle
    "git diff -RO../../order",
    "git log --outp=x.txt",         # abbreviation of --output
    "git diff --textc",             # abbreviation of --textconv
    "git diff --no-i ../a ../b",    # abbreviation of --no-index
])
def test_git_option_bundles_and_abbreviations_are_refused(tools, command):
    assert "not allowed" in tools.execute("run_command", {"command": command})


@pytest.mark.parametrize("command", ["git diff --text", "git log --oneline -5", "git diff --stat", "git show --name-only"])
def test_ordinary_git_options_still_pass_the_filter(tools, command):
    assert tools._allowed_argv(command)[:2] == ["git", command.split()[1]]


# ── R-07 / N-01 ─────────────────────────────────────────────────────────────

def test_read_only_tool_list_names_only_real_tools():
    from harness.runners.base import default_read_only_tools

    assert "LS" not in default_read_only_tools(HarnessConfig(project_name="p", brief="b"))


@pytest.mark.parametrize("kwargs", [
    {"active_runner_env": {"APIKEY": "abc123"}},
    {"active_runner_env": {"GHTOKEN": "abc"}},
    {"active_runner_env": {"AUTH": "Bearer abc"}},
    {"active_runner_env": {"CREDENTIALS": "abc"}},
    {"active_runner_env": {"DATABASE_URL": "postgres://app:hunter2@db/app"}},
    {"active_runner_env": {"ANYTHING": "sk-ant-api03-abcdefghijklmnopqrstu"}},
    {"code_runner_extra_args": ["--settings", json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-abcdefghijklmnopqrst"}})]},
    {"code_runner_extra_args": ["-c", "model_providers.openrouter.api_key=literal-value-123"]},
    {"code_runner_extra_args": ["--apikey", "abc"]},
])
def test_widened_secret_literals_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        HarnessConfig(project_name="p", brief="b", **kwargs)


def test_widened_checks_leave_ordinary_settings_alone():
    HarnessConfig(
        project_name="p", brief="b",
        active_runner_env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000", "GIT_AUTHOR_NAME": "Agent Harness",
                           "OPENAI_BASE_URL": "https://openrouter.ai/api/v1", "APIKEY": "$APIKEY"},
        code_runner_extra_args=["--max-tokens", "8192", "--verbose",
                                "--settings", json.dumps({"env": {"ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"}})],
    )
