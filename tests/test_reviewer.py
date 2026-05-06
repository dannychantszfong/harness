"""Tests for ReviewerAgent and the orchestrator's review-only path."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from harness.config import HarnessConfig
from harness.agents.reviewer import ReviewerAgent
from harness.orchestrator import Orchestrator
from harness.runners.base import PreflightResult, RunnerType
from harness.progress.models import Feature, FeatureStatus, ProjectProgress
from harness.progress.tracker import ProgressTracker


def _make_runner_config(tmp_path: Path) -> HarnessConfig:
    cfg = HarnessConfig(
        project_name="ReviewMe",
        project_id="review01",
        brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
        code_runner="subprocess",
    )
    cfg.save_yaml(tmp_path / "config.yaml")
    return cfg


# ── ReviewerAgent.review() ──────────────────────────────────────────────────

def test_reviewer_runner_reads_review_md_from_disk(tmp_path):
    """When the runner writes REVIEW.md to disk, agent reads it back."""
    cfg = _make_runner_config(tmp_path)
    fake_runner = MagicMock()
    fake_runner.implement.return_value = MagicMock(
        success=True, output="REVIEW_DONE", error=None,
        rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
    )

    review_md = (tmp_path / "REVIEW.md")

    def write_review(prompt, cwd, timeout_seconds=600):
        review_md.write_text("# REVIEW\n\nAll good.")
        return MagicMock(
            success=True, output="REVIEW_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
        )

    fake_runner.implement.side_effect = write_review

    reviewer = ReviewerAgent(cfg, runner=fake_runner)
    result = reviewer.review()

    assert "All good" in result
    assert review_md.exists()


def test_reviewer_runner_falls_back_to_review_tag(tmp_path):
    """When no file is written but agent prints <review>...</review>, we accept it."""
    cfg = _make_runner_config(tmp_path)
    fake_runner = MagicMock()
    fake_runner.implement.return_value = MagicMock(
        success=True,
        output="<review>\n# REVIEW\nInline body.\n</review>",
        error=None, rate_limit_reset_at=None,
        input_tokens=None, output_tokens=None,
    )

    reviewer = ReviewerAgent(cfg, runner=fake_runner)
    result = reviewer.review()

    assert "Inline body" in result
    assert (tmp_path / "REVIEW.md").exists()


def test_reviewer_runner_no_output_raises_with_context(tmp_path):
    cfg = _make_runner_config(tmp_path)
    fake_runner = MagicMock()
    fake_runner.implement.return_value = MagicMock(
        success=True, output="random unrelated output text", error=None,
        rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
    )

    reviewer = ReviewerAgent(cfg, runner=fake_runner)
    with pytest.raises(RuntimeError) as exc:
        reviewer.review()
    assert "REVIEW.md" in str(exc.value)
    assert "random unrelated" in str(exc.value)


def test_reviewer_includes_spec_and_features_in_prompt(tmp_path):
    cfg = _make_runner_config(tmp_path)
    (tmp_path / "spec.md").write_text("# Spec\nThe spec.")
    progress = ProjectProgress(
        project_name="ReviewMe", brief="b",
        features=[
            Feature(id="f1", name="Auth", description="x", priority=0,
                    status=FeatureStatus.PASSING),
            Feature(id="f2", name="UI", description="y", priority=1,
                    status=FeatureStatus.PENDING),
        ],
    )
    captured = {}
    fake_runner = MagicMock()

    def capture(prompt, cwd, timeout_seconds=600):
        captured["prompt"] = prompt
        (tmp_path / "REVIEW.md").write_text("ok")
        return MagicMock(
            success=True, output="REVIEW_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
        )

    fake_runner.implement.side_effect = capture
    ReviewerAgent(cfg, runner=fake_runner).review(progress=progress)

    prompt = captured["prompt"]
    assert "The spec." in prompt        # spec.md included
    assert "f1" in prompt and "Auth" in prompt and "passing" in prompt
    assert "f2" in prompt and "UI" in prompt and "pending" in prompt
    assert "1/2 passing" in prompt or "1/2" in prompt


# ── Orchestrator review_only=True path ─────────────────────────────────────

def test_orchestrator_review_only_skips_init_plan_loop(tmp_path, monkeypatch):
    """run(review_only=True) must call ReviewerAgent.review(), not _initialize/_plan/_feature_loop."""
    cfg = _make_runner_config(tmp_path)

    fake_runner = MagicMock()
    fake_runner.preflight.return_value = PreflightResult(
        ok=True, summary="stub", details="stub"
    )
    monkeypatch.setattr(
        "harness.orchestrator.create_runner", lambda *a, **k: fake_runner
    )

    calls = {"init": False, "plan": False, "loop": False, "review": False}

    def fail_init(self): calls["init"] = True; raise AssertionError("init called")
    def fail_plan(self, *a, **k): calls["plan"] = True; raise AssertionError("plan called")
    def fail_loop(self, *a, **k): calls["loop"] = True; raise AssertionError("loop called")

    monkeypatch.setattr(Orchestrator, "_initialize", fail_init)
    monkeypatch.setattr(Orchestrator, "_plan", fail_plan)
    monkeypatch.setattr(Orchestrator, "_feature_loop", fail_loop)

    def fake_review(self, progress=None):
        calls["review"] = True
        (Path(self.config.output_dir) / "REVIEW.md").write_text("# fake")
        return "# fake"

    monkeypatch.setattr(ReviewerAgent, "review", fake_review)

    orch = Orchestrator(cfg, runner_type=RunnerType.SUBPROCESS)
    orch.run(review_only=True)

    assert calls["review"] is True
    assert calls["init"] is False
    assert calls["plan"] is False
    assert calls["loop"] is False
    assert (tmp_path / "REVIEW.md").exists()


def test_orchestrator_review_only_handles_rate_limit_gracefully(tmp_path, monkeypatch):
    """A rate-limit during review must surface as the friendly panel, not a crash."""
    from datetime import datetime, timedelta, timezone
    from harness.runners.base import RunnerRateLimitedError

    cfg = _make_runner_config(tmp_path)
    fake_runner = MagicMock()
    fake_runner.preflight.return_value = PreflightResult(
        ok=True, summary="stub", details="stub"
    )
    monkeypatch.setattr(
        "harness.orchestrator.create_runner", lambda *a, **k: fake_runner
    )

    def boom(self, progress=None):
        raise RunnerRateLimitedError(
            reset_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            raw_message="hit limit",
        )

    monkeypatch.setattr(ReviewerAgent, "review", boom)

    # Disable auto-resume so we don't actually load a launchd job
    cfg.auto_resume_on_rate_limit = False

    orch = Orchestrator(cfg, runner_type=RunnerType.SUBPROCESS)
    # Should NOT raise
    orch.run(review_only=True)
