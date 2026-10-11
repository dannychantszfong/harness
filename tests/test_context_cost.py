"""Context resets on real token counts (C-01), cost tracking (C-04), and
handoff scoping (P-27). Real agents run against a fake runner that reports
usage the way the coding-agent runners now do."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from harness.config import CONFIG_FILENAME, HarnessConfig, RunnerProfile
from harness.context.handoff import HandoffDocument
from harness.orchestrator import Orchestrator
from harness.progress.models import Feature, FeatureStatus, ProjectProgress
from harness.progress.tracker import ProgressTracker, usage_summary
from harness.runners.base import PreflightResult, RunResult, RunnerType
from harness.timeutil import utcnow

pytestmark = pytest.mark.usefixtures("runners_ready")


def _evaluation(score: int) -> str:
    return (
        f"<evaluation><design_quality>{score}</design_quality><originality>{score}</originality>"
        f"<craft>{score}</craft><functionality>{score}</functionality>"
        f"<feedback>score was {score}</feedback></evaluation>"
    )


class ScriptedRunner:
    """Stands in for a coding-agent runner; every session reports usage."""

    runner_type = RunnerType.SUBPROCESS

    def __init__(self, eval_scores, tokens_in=60, tokens_out=20, cost=0.05):
        self.eval_scores = list(eval_scores)
        self.tokens_in, self.tokens_out, self.cost = tokens_in, tokens_out, cost
        self.prompts: list[tuple[str, str]] = []

    def preflight(self):
        return PreflightResult(ok=True, summary="scripted", details="")

    def implement(self, prompt, cwd, timeout_seconds=None, access=None):
        if "<sprint_contract>" in prompt:
            kind, output = "contract", "no contract block"
        elif "adversarial QA evaluator" in prompt:
            kind, output = "evaluator", _evaluation(self.eval_scores.pop(0))
        else:
            kind, output = "generator", "self evaluation"
        self.prompts.append((kind, prompt))
        return RunResult(output=output, success=True, input_tokens=self.tokens_in,
                         output_tokens=self.tokens_out, cost_usd=self.cost)


def _project(tmp_path, features, **kw) -> HarnessConfig:
    cfg = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path), orchestration_mode="runner",
                        code_runner="subprocess", **kw)
    cfg.save_file(tmp_path / CONFIG_FILENAME)
    ProgressTracker(cfg).save(ProjectProgress(project_name="p", brief="b", spec="spec", features=features,
                                              session_count=1))
    return cfg


def _run(cfg, runner, monkeypatch):
    monkeypatch.setattr("harness.orchestrator.create_runner", lambda *a, **k: runner)
    orch = Orchestrator(cfg)
    progress = ProgressTracker(cfg).load()
    orch._initialize = lambda **k: progress
    orch._plan = lambda p, **k: p
    orch.run()
    return orch


def _generator_prompts(runner):
    return [p for kind, p in runner.prompts if kind == "generator"]


# ── C-01: a run that crosses the threshold resets with a handoff ────────────

def test_context_reset_fires_on_real_runner_usage(tmp_path, monkeypatch):
    # 80 tokens per session: contract + gen + eval = 240 ≥ 200 → reset after
    # iteration 1; gen + eval of iteration 2 = 160 < 200 → no seam reset.
    cfg = _project(tmp_path, [Feature(id="f1", name="Login", description="a")],
                   context_reset_threshold_tokens=200, max_iterations_per_feature=3)
    runner = ScriptedRunner(eval_scores=[4, 9])
    statuses = []
    original = Orchestrator._do_context_reset

    def spy(self, progress, feature_id, feedback):
        before = ProgressTracker(cfg).load().get_feature(feature_id).status
        handoff = original(self, progress, feature_id, feedback)
        statuses.append((before, ProgressTracker(cfg).load().get_feature(feature_id).status))
        return handoff

    monkeypatch.setattr(Orchestrator, "_do_context_reset", spy)
    _run(cfg, runner, monkeypatch)

    handoff_path = tmp_path / "handoff_session_0002.json"
    assert handoff_path.exists()
    handoff = json.loads(handoff_path.read_text())
    assert handoff["feature_id"] == "f1"
    gen = _generator_prompts(runner)
    assert len(gen) == 2
    assert "Handoff from Session 2" not in gen[0]
    assert "Handoff from Session 2" in gen[1]  # the following iteration sees it
    # The reset changes no feature status as a side effect
    assert statuses == [(FeatureStatus.FAILING, FeatureStatus.FAILING)]
    final = ProgressTracker(cfg).load()
    assert final.get_feature("f1").status == FeatureStatus.PASSING
    assert final.session_count == 2


def test_no_reset_below_threshold(tmp_path, monkeypatch):
    cfg = _project(tmp_path, [Feature(id="f1", name="Login", description="a")],
                   context_reset_threshold_tokens=10_000)
    _run(cfg, ScriptedRunner(eval_scores=[4, 9]), monkeypatch)
    assert not list(tmp_path.glob("handoff_session_*.json"))


def test_seam_reset_handoff_goes_to_the_next_feature_only(tmp_path, monkeypatch):
    # 240 tokens per passing feature; threshold 400 → one seam reset after
    # f2 (480), a second after f4.
    cfg = _project(
        tmp_path,
        [Feature(id=f"f{i}", name=f"F{i}", description="d", priority=i) for i in (1, 2, 3, 4)],
        context_reset_threshold_tokens=400,
    )
    runner = ScriptedRunner(eval_scores=[9, 9, 9, 9])
    _run(cfg, runner, monkeypatch)

    first = json.loads((tmp_path / "handoff_session_0002.json").read_text())
    assert first["feature_id"] is None
    assert "F3" in first["next_action"]
    gen = _generator_prompts(runner)
    assert ["Handoff from Session" in p for p in gen] == [False, False, True, False]
    # P-27: the between-features handoff is not carried into f4


def test_old_handoff_about_another_feature_is_not_injected(tmp_path, monkeypatch):
    cfg = _project(tmp_path, [Feature(id="f9", name="Later", description="d")])
    HandoffDocument(project_name="p", session_number=4, completed_at=utcnow(), what_was_done="worked on f3",
                    current_state="s", next_action="n", feature_id="f3").save(tmp_path)
    runner = ScriptedRunner(eval_scores=[9])
    _run(cfg, runner, monkeypatch)
    assert "worked on f3" not in _generator_prompts(runner)[0]


def test_resumed_run_continues_session_numbering(tmp_path, monkeypatch):
    cfg = _project(tmp_path, [Feature(id="f1", name="A", description="a")],
                   context_reset_threshold_tokens=200, max_iterations_per_feature=3)
    progress = ProgressTracker(cfg).load()
    progress.session_count = 5
    ProgressTracker(cfg).save(progress)
    _run(cfg, ScriptedRunner(eval_scores=[4, 9]), monkeypatch)
    assert (tmp_path / "handoff_session_0006.json").exists()


# ── C-04: tokens and cost are recorded and shown ────────────────────────────

def test_usage_is_accumulated_per_feature_and_project(tmp_path, monkeypatch):
    cfg = _project(tmp_path, [Feature(id="f1", name="A", description="a")], context_reset_threshold_tokens=10**9)
    _run(cfg, ScriptedRunner(eval_scores=[4, 9], cost=0.05), monkeypatch)

    final = ProgressTracker(cfg).load()
    sessions = 5  # contract, gen, eval, gen, eval
    assert final.total_tokens_used == sessions * 80
    assert final.total_cost_usd == pytest.approx(sessions * 0.05)
    assert final.get_feature("f1").tokens_used == sessions * 80
    md = cfg.progress_path.read_text(encoding="utf-8")
    assert "400 tokens" in md and "cost: subscription" in md


def test_status_shows_tokens_and_cost(tmp_path):
    cfg = _project(tmp_path, [Feature(id="f1", name="A", description="a", status=FeatureStatus.PASSING,
                                      tokens_used=1234, cost_usd=0.5)])
    progress = ProgressTracker(cfg).load()
    progress.total_tokens_used, progress.total_cost_usd = 1234, 0.5
    ProgressTracker(cfg).save(progress)

    from harness import cli
    out = CliRunner().invoke(cli.main, ["status", str(tmp_path)]).output
    assert "Usage: 1,234 tokens · cost: subscription (≈$0.50 at API prices)" in out
    assert "1,234 tok" in out


def test_usage_summary_by_billing_mode(tmp_path):
    sub = HarnessConfig(project_name="p", brief="b", orchestration_mode="runner")
    assert usage_summary(10, 0.0, sub) == "10 tokens · cost: subscription"
    api = HarnessConfig(project_name="p", brief="b", orchestration_mode="runner",
                        runner_profiles=[RunnerProfile(name="or", runner="subprocess", provider="openrouter")])
    assert api.billing_mode == "api"
    assert usage_summary(10, 1.5, api) == "10 tokens · $1.50"
    assert usage_summary(10, 0.0, api) == "10 tokens · cost: not reported"


def test_agent_usage_counts_cost(tmp_path):
    from harness.agents.evaluator import EvaluatorAgent

    cfg = HarnessConfig(project_name="p", brief="b", output_dir=str(tmp_path), orchestration_mode="runner")
    runner = MagicMock()
    runner.implement.return_value = RunResult(output=_evaluation(7), success=True, input_tokens=100,
                                              output_tokens=50, cache_read_tokens=999, cost_usd=0.2)
    agent = EvaluatorAgent(cfg, runner=runner)
    agent.evaluate(Feature(id="f1", name="A", description="a"), "s", 1)
    assert agent.usage.total_tokens == 150  # cache reads are not new context
    assert agent.usage.cost_usd == pytest.approx(0.2)


def test_final_panel_shows_usage(tmp_path, monkeypatch):
    cfg = _project(tmp_path, [Feature(id="f1", name="A", description="a")])
    orch = _run(cfg, ScriptedRunner(eval_scores=[9]), monkeypatch)
    panel = orch._final_panel(ProgressTracker(cfg).load())
    assert "Usage:" in str(panel.renderable) and "tokens" in str(panel.renderable)


def test_handoff_round_trips_feature_id(tmp_path):
    HandoffDocument(project_name="p", session_number=2, completed_at=utcnow(), what_was_done="w",
                    current_state="c", next_action="n", feature_id="f7").save(tmp_path)
    assert HandoffDocument.load_latest(Path(tmp_path)).feature_id == "f7"
