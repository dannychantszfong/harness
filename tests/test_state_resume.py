"""State and resume (Phase B): P-01..P-04, C-02, C-09, C-10.

Git-backed tests use a real `git` in tmp dirs (CI has git; no identity is
needed — Harness falls back to its own). Agents are mocked.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from harness.config import CONFIG_FILENAME, HarnessConfig
from harness.orchestrator import Orchestrator
from harness.progress.models import (
    EvaluationResult,
    Feature,
    FeatureStatus,
    ProjectProgress,
    SprintContract,
    dedupe_feature_ids,
    normalize_feature_entries,
)
from harness.progress.tracker import CorruptFeaturesFileError, ProgressTracker
from harness.project_git import git_head, is_git_repo

pytestmark = pytest.mark.usefixtures("runners_ready")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _eval(score: float, passed: bool, iteration: int = 1, functionality: float | None = None) -> EvaluationResult:
    return EvaluationResult(
        design_quality=score, originality=score, craft=score,
        functionality=score if functionality is None else functionality,
        overall_score=score, feedback=f"feedback at {score}", passed=passed, iteration=iteration,
    )


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout.strip()


def _project(tmp_path: Path, features: list[Feature], *, git: bool = False, **cfg) -> HarnessConfig:
    project = tmp_path / "proj"
    project.mkdir()
    config = HarnessConfig(
        project_name="p", brief="b", output_dir=str(project),
        orchestration_mode="runner", context_reset_threshold_tokens=10**9,
        sprint_contract_enabled=False, **cfg,
    )
    config.save_file(project / CONFIG_FILENAME)
    ProgressTracker(config).save(ProjectProgress(project_name="p", brief="b", spec="spec", features=features))
    if git:
        _git(project, "init", "-q")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "-m", "init")
    return config


def _run_loop(config, generate, evaluations):
    """Run the feature loop with a scripted generator and evaluator."""
    orch = Orchestrator(config)
    progress = ProgressTracker(config).load()
    with (
        patch.object(orch, "_initialize", return_value=progress),
        patch.object(orch, "_plan", return_value=progress),
        patch("harness.orchestrator.GeneratorAgent") as MockGen,
        patch("harness.orchestrator.EvaluatorAgent") as MockEval,
    ):
        gen = MockGen.return_value
        gen.implement_feature.side_effect = generate
        gen.usage = MagicMock(total_tokens=0, cost_usd=0.0)
        ev = MockEval.return_value
        ev.evaluate.side_effect = evaluations
        ev.usage = MagicMock(total_tokens=0, cost_usd=0.0)
        orch.run()
    return gen, ev, orch


# ── P-01: relative output_dir / resume from another cwd ─────────────────────

def test_save_file_persists_absolute_output_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = HarnessConfig(project_name="p", brief="b", output_dir="./output/x")
    cfg.save_file(tmp_path / "c.json")
    saved = json.loads((tmp_path / "c.json").read_text())["output_dir"]
    assert Path(saved).is_absolute()
    assert Path(saved) == (tmp_path / "output" / "x").resolve()


def test_resume_with_relative_output_dir_from_project_cwd(tmp_path, monkeypatch):
    """The scheduler cds into the project before `harness resume <dir>`; a
    relative output_dir used to resolve to <project>/output/<slug> and restart
    the initializer in an empty nested directory."""
    project = tmp_path / "output" / "app_1234"
    project.mkdir(parents=True)
    (project / CONFIG_FILENAME).write_text(json.dumps({
        "project_name": "App", "brief": "b", "output_dir": "./output/app_1234",
        "orchestration_mode": "runner", "code_runner": "subprocess",
    }))
    captured = {}

    class FakeOrch:
        def __init__(self, config, runner_type=None):
            captured["config"] = config

        def run(self, **kwargs):
            captured["ran"] = True

    from harness import cli
    monkeypatch.setattr(cli, "Orchestrator", FakeOrch)
    monkeypatch.setattr(cli, "load_setup", lambda *a, **k: None)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli.main, ["resume", str(project)])

    assert result.exit_code == 0, result.output
    assert Path(captured["config"].output_dir) == project.resolve()
    assert not (project / "output").exists()
    stored = json.loads((project / CONFIG_FILENAME).read_text())["output_dir"]
    assert Path(stored) == project.resolve()


def test_status_accepts_project_directory(tmp_path, monkeypatch):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")])
    monkeypatch.chdir(tmp_path)
    from harness import cli
    result = CliRunner().invoke(cli.main, ["status", config.output_dir])
    assert result.exit_code == 0, result.output
    assert "[f1] A" in result.output


# ── P-02: interrupted and failing features are retried ──────────────────────

def test_next_actionable_feature_order():
    p = ProjectProgress(project_name="p", brief="b", features=[
        Feature(id="pend", name="P", description="p", priority=0),
        Feature(id="fail", name="F", description="f", priority=1, status=FeatureStatus.FAILING,
                evaluation_history=[_eval(5, False)]),
        Feature(id="int", name="I", description="i", priority=5, status=FeatureStatus.IN_PROGRESS),
        Feature(id="done", name="D", description="d", priority=0, status=FeatureStatus.PASSING),
        Feature(id="exh", name="E", description="e", priority=0, status=FeatureStatus.FAILING,
                evaluation_history=[_eval(5, False, i) for i in range(3)]),
    ])
    assert p.next_actionable_feature(3).id == "int"  # interrupted first
    p.get_feature("int").status = FeatureStatus.PASSING
    assert p.next_actionable_feature(3).id == "pend"
    p.get_feature("pend").status = FeatureStatus.PASSING
    assert p.next_actionable_feature(3).id == "fail"  # budget left → retried
    p.get_feature("fail").status = FeatureStatus.PASSING
    assert p.next_actionable_feature(3) is None  # exhausted never returned
    assert [f.id for f in p.exhausted_features(3)] == ["exh"]
    assert p.next_actionable_feature(5).id == "exh"  # raised budget → retried


def test_interrupted_feature_resumes_where_it_stopped(tmp_path):
    feature = Feature(id="f1", name="A", description="a", status=FeatureStatus.IN_PROGRESS,
                      evaluation_history=[_eval(5, False, 1)])
    config = _project(tmp_path, [feature], max_iterations_per_feature=3)

    gen, ev, _ = _run_loop(config, lambda **k: "self eval", [_eval(9, True, 2)])

    assert gen.implement_feature.call_count == 1
    call = gen.implement_feature.call_args.kwargs
    assert call["iteration"] == 2
    assert call["evaluator_feedback"] == "feedback at 5"  # carried over
    assert ProgressTracker(config).load().get_feature("f1").status == FeatureStatus.PASSING


def test_budget_is_per_feature_across_runs(tmp_path):
    feature = Feature(id="f1", name="A", description="a", status=FeatureStatus.FAILING,
                      evaluation_history=[_eval(5, False, i) for i in (1, 2)])
    config = _project(tmp_path, [feature], max_iterations_per_feature=3)

    gen, _, orch = _run_loop(config, lambda **k: "x", [_eval(5, False, 3)])

    assert gen.implement_feature.call_count == 1  # only the one remaining
    final = ProgressTracker(config).load()
    assert final.get_feature("f1").is_exhausted(3)
    panel = orch._final_panel(final)
    assert "1 feature exhausted retries" in str(panel.renderable)
    assert ".harness/attempts/f1.md" in str(panel.renderable)


# ── P-03: corrupt features.json is backed up, never overwritten ─────────────

@pytest.mark.parametrize("content", ['{"features": [', '{"project_name": "p"}', '"just a string"'])
def test_corrupt_features_file_backed_up_and_aborts(tmp_path, content):
    from harness.agents.initializer import InitializerAgent

    project = tmp_path / "proj"
    project.mkdir()
    config = HarnessConfig(project_name="p", brief="b", output_dir=str(project), orchestration_mode="runner")
    config.features_path.write_text(content)
    runner = MagicMock()

    with pytest.raises(CorruptFeaturesFileError) as exc:
        InitializerAgent(config, runner=runner).run(brief="b")

    runner.implement.assert_not_called()  # no re-decomposition
    assert config.features_path.read_text() == content  # untouched
    backups = list(project.glob("features.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text() == content
    assert "will not overwrite" in str(exc.value)


def test_cli_reports_corrupt_progress_without_traceback(tmp_path, monkeypatch):
    from harness import cli
    from harness.progress.tracker import CorruptFeaturesFileError as Err

    class FakeOrch:
        def __init__(self, *a, **k):
            pass

        def run(self, **k):
            raise Err(Path("features.json"), "invalid JSON", Path("features.json.corrupt-1"))

    project = tmp_path / "proj"
    project.mkdir()
    HarnessConfig(project_name="p", brief="b", output_dir=str(project), orchestration_mode="runner",
                  code_runner="subprocess").save_file(project / CONFIG_FILENAME)
    monkeypatch.setattr(cli, "Orchestrator", FakeOrch)
    monkeypatch.setattr(cli, "load_setup", lambda *a, **k: None)
    result = CliRunner().invoke(cli.main, ["resume", str(project)])
    assert result.exit_code == 1
    assert "Progress file problem" in result.output
    assert "Traceback" not in result.output


# ── P-04: malformed and duplicate entries ───────────────────────────────────

def test_normalize_feature_entries_repairs_or_drops():
    entries, warnings = normalize_feature_entries([
        {"name": "Login"},                                # no id, no description
        {"id": "f2", "description": "Profile page"},      # no name
        {"id": "f3", "name": "Bad prio", "description": "x", "priority": "high"},
        {"id": "f4"},                                     # unusable
        "not a dict",
    ])
    assert [e["id"] for e in entries] == ["f1", "f2", "f3"]
    assert entries[0]["description"] == "Login"
    assert entries[1]["name"] == "Profile page"
    assert entries[2]["priority"] == 2
    assert len(warnings) >= 5
    with pytest.raises(ValueError):
        normalize_feature_entries([{"id": "x"}])


def test_duplicate_ids_are_renamed_deterministically():
    features = [Feature(id=i, name=i, description=i) for i in ("f1", "f1", "f1-2", "f1")]
    renames = dedupe_feature_ids(features)
    assert [f.id for f in features] == ["f1", "f1-3", "f1-2", "f1-4"]
    assert renames == [("f1", "f1-3"), ("f1", "f1-4")]


def test_tracker_load_dedupes_and_persists(tmp_path):
    config = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path))
    config.features_path.write_text(json.dumps({
        "project_name": "p", "brief": "b",
        "features": [{"id": "a", "name": "A", "description": "1"}, {"id": "a", "name": "B", "description": "2"}],
    }))
    progress = ProgressTracker(config).load()
    assert [f.id for f in progress.features] == ["a", "a-2"]
    assert progress.get_feature("a-2").name == "B"
    on_disk = json.loads(config.features_path.read_text())
    assert [f["id"] for f in on_disk["features"]] == ["a", "a-2"]


def test_runner_bare_list_with_missing_fields_initializes(tmp_path):
    from harness.agents.initializer import InitializerAgent

    config = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path), orchestration_mode="runner")
    config.features_path.write_text(json.dumps([{"name": "Login"}, {"name": "Login again", "id": "f1"}]))
    progress = InitializerAgent(config, runner=MagicMock()).run(brief="b")
    assert [f.id for f in progress.features] == ["f1", "f1-2"]


# ── C-02: Harness-owned commits and exhausted attempts ──────────────────────

@needs_git
def test_passing_feature_gets_harness_commit(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="Login", description="a")], git=True)
    project = Path(config.output_dir)
    base = git_head(project)

    def generate(**kwargs):
        (project / "login.py").write_text("def login(): ...\n")  # never committed by the "agent"
        return "done"

    _run_loop(config, generate, [_eval(9, True)])

    feature = ProgressTracker(config).load().get_feature("f1")
    # The base is HEAD after Harness committed what was on disk (here only
    # its own .gitignore entries) — R-01.
    assert _git(project, "merge-base", "--is-ancestor", base, feature.base_commit_sha) == ""
    assert "login.py" not in _git(project, "ls-tree", "-r", "--name-only", feature.base_commit_sha)
    assert feature.commit_sha is not None and feature.commit_sha != feature.base_commit_sha
    assert _git(project, "log", "-1", "--format=%s", feature.commit_sha) == "feat(f1): Login — score 9.0"
    assert "login.py" in _git(project, "show", "--name-only", "--format=", feature.commit_sha)


@needs_git
def test_exhausted_feature_is_preserved_on_branch_and_reverted(tmp_path):
    config = _project(
        tmp_path,
        [Feature(id="f1", name="Login", description="a"), Feature(id="f2", name="Next", description="b", priority=1)],
        git=True,
        max_iterations_per_feature=2,
    )
    project = Path(config.output_dir)
    base = git_head(project)
    seen_files = []

    def generate(feature, **kwargs):
        if feature.id == "f1":
            (project / "broken.py").write_text(f"attempt {kwargs['iteration']}\n")
            return "f1 self-eval: still broken"
        seen_files.append((project / "broken.py").exists())
        return "f2 ok"

    _run_loop(config, generate, [_eval(4, False, 1), _eval(5, False, 2), _eval(9, True, 1)])

    progress = ProgressTracker(config).load()
    f1 = progress.get_feature("f1")
    assert f1.attempt_branches == ["harness/attempt/f1/1"]
    assert f1.is_exhausted(2)
    assert len(f1.evaluation_history) == 2  # state survived the reset
    # The attempt's code lives on the branch …
    assert _git(project, "show", "harness/attempt/f1/1:broken.py") == "attempt 2"
    # … but the next feature started from the clean baseline
    assert seen_files == [False]
    assert _git(project, "merge-base", "--is-ancestor", base, "HEAD") == ""
    notes = (project / ".harness" / "attempts" / "f1.md").read_text(encoding="utf-8")
    assert "harness/attempt/f1/1" in notes
    assert "f1 self-eval: still broken" in notes
    assert "feedback at 4" in notes and "Weakest dimension" in notes
    assert "Exhausted features" in config.progress_path.read_text(encoding="utf-8")
    assert progress.get_feature("f2").status == FeatureStatus.PASSING


@needs_git
def test_retry_of_exhausted_feature_injects_lessons(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="Login", description="a")], git=True,
                      max_iterations_per_feature=1)
    _run_loop(config, lambda **k: "attempt one failed", [_eval(3, False, 1)])

    config.max_iterations_per_feature = 2  # owner raises the budget, then resumes
    config.save_file(Path(config.output_dir) / CONFIG_FILENAME)
    gen, _, _ = _run_loop(config, lambda **k: "ok", [_eval(9, True, 2)])

    lessons = gen.implement_feature.call_args.kwargs["lessons"]
    assert "attempt one failed" in lessons
    assert "harness/attempt/f1/1" in lessons


def test_generator_prompt_renders_lessons_and_state_rule():
    from harness.agents.generator import _build_implementation_prompt

    feature = Feature(id="f1", name="A", description="d",
                      sprint_contract=SprintContract(feature_id="f1", acceptance_criteria=["c1"], out_of_scope=["o1"]))
    prompt = _build_implementation_prompt(feature, "pre", None, 1, lessons="LESSON TEXT")
    assert "Lessons from previous attempts" in prompt and "LESSON TEXT" in prompt
    assert "features.json" in prompt and "reverted" in prompt
    assert "o1" in prompt


@needs_git
def test_is_git_repo_requires_project_root(tmp_path):
    _git(tmp_path, "init", "-q")
    nested = tmp_path / "output" / "proj"
    nested.mkdir(parents=True)
    assert is_git_repo(tmp_path) is True
    assert is_git_repo(nested) is False  # never operate on the parent repo
    assert git_head(nested) is None


# ── C-09: resume says what it resumes ───────────────────────────────────────

def test_resume_prints_summary(tmp_path, monkeypatch):
    from harness import cli

    features = [
        Feature(id="f1", name="Done", description="a", status=FeatureStatus.PASSING),
        Feature(id="f2", name="Half way", description="b", priority=1, status=FeatureStatus.IN_PROGRESS,
                evaluation_history=[_eval(5, False)]),
        Feature(id="f3", name="Hopeless", description="c", priority=2, status=FeatureStatus.FAILING,
                evaluation_history=[_eval(2, False, i) for i in range(3)]),
        Feature(id="f4", name="Later", description="d", priority=3),
    ]
    config = _project(tmp_path, features, max_iterations_per_feature=3)

    class FakeOrch:
        def __init__(self, *a, **k):
            pass

        def run(self, **k):
            pass

    monkeypatch.setattr(cli, "Orchestrator", FakeOrch)
    monkeypatch.setattr(cli, "load_setup", lambda *a, **k: None)
    out = CliRunner().invoke(cli.main, ["resume", config.output_dir, "-r", "subprocess"]).output

    assert "1 in progress" in out and "1 pending" in out and "1 exhausted retries" in out
    assert "Picks up: [f2] Half way (interrupted, iteration 2/3)" in out
    assert "exhausted: [f3] Hopeless" in out
    assert "Handoff: none" in out


# ── C-10: agents cannot rewrite Harness state ───────────────────────────────

def test_agent_edit_of_features_json_is_reverted(tmp_path):
    config = _project(tmp_path, [Feature(id="f1", name="A", description="a")], max_iterations_per_feature=3)

    def generate(**kwargs):
        # A misbehaving agent marks everything passing.
        data = json.loads(config.features_path.read_text())
        data["features"][0]["status"] = "passing"
        data["features"][0]["name"] = "HACKED"
        config.features_path.write_text(json.dumps(data))
        return "done"

    _run_loop(config, generate, [_eval(5, False, i) for i in (1, 2, 3)])

    f1 = ProgressTracker(config).load().get_feature("f1")
    assert f1.name == "A"
    assert f1.status == FeatureStatus.FAILING


def test_protect_state_restores_even_when_call_raises(tmp_path):
    config = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path))
    tracker = ProgressTracker(config)
    tracker.save(ProjectProgress(project_name="p", brief="b"))
    original = config.features_path.read_bytes()
    with pytest.raises(RuntimeError):
        with tracker.protect_state():
            config.features_path.write_text("garbage")
            raise RuntimeError("rate limited mid-call")
    assert config.features_path.read_bytes() == original
