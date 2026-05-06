"""Tests for the progress tracker and feature models."""

import json
import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from harness.config import HarnessConfig
from harness.progress.models import Feature, FeatureStatus, ProjectProgress, EvaluationResult
from harness.progress.tracker import ProgressTracker


@pytest.fixture
def tmp_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="test-project",
        brief="A test project",
        output_dir=str(tmp_path),
    )


@pytest.fixture
def sample_features() -> list[Feature]:
    return [
        Feature(id="f1", name="User auth", description="Login and signup", priority=0),
        Feature(id="f2", name="Dashboard", description="Main dashboard view", priority=1),
        Feature(id="f3", name="Settings", description="User settings page", priority=2),
    ]


class TestFeatureModel:
    def test_default_status_is_pending(self):
        f = Feature(id="x", name="X", description="desc", priority=0)
        assert f.status == FeatureStatus.PENDING

    def test_iteration_count(self):
        f = Feature(id="x", name="X", description="desc", priority=0)
        assert f.iteration_count == 0
        f.evaluation_history.append(
            EvaluationResult(
                design_quality=7,
                originality=7,
                craft=7,
                functionality=7,
                overall_score=7,
                feedback="ok",
                passed=False,
                iteration=1,
            )
        )
        assert f.iteration_count == 1

    def test_latest_evaluation_none_when_empty(self):
        f = Feature(id="x", name="X", description="desc", priority=0)
        assert f.latest_evaluation is None


class TestProjectProgress:
    def test_completion_pct_zero_when_no_features(self):
        p = ProjectProgress(project_name="p", brief="b")
        assert p.completion_pct == 0.0

    def test_completion_pct(self, sample_features):
        p = ProjectProgress(project_name="p", brief="b", features=sample_features)
        p.features[0].status = FeatureStatus.PASSING
        assert p.completion_pct == pytest.approx(33.3, abs=0.1)

    def test_next_pending_feature_returns_lowest_priority(self, sample_features):
        p = ProjectProgress(project_name="p", brief="b", features=sample_features)
        p.features[0].status = FeatureStatus.PASSING
        nxt = p.next_pending_feature()
        assert nxt is not None
        assert nxt.id == "f2"

    def test_next_pending_feature_none_when_all_pass(self, sample_features):
        p = ProjectProgress(project_name="p", brief="b", features=sample_features)
        for f in p.features:
            f.status = FeatureStatus.PASSING
        assert p.next_pending_feature() is None


class TestProgressTracker:
    def test_save_and_load_roundtrip(self, tmp_config, sample_features):
        tracker = ProgressTracker(tmp_config)
        progress = ProjectProgress(
            project_name="test",
            brief="test brief",
            features=sample_features,
        )
        tracker.save(progress)
        loaded = tracker.load()
        assert loaded.project_name == "test"
        assert len(loaded.features) == 3

    def test_load_raises_when_missing(self, tmp_config):
        tracker = ProgressTracker(tmp_config)
        with pytest.raises(FileNotFoundError):
            tracker.load()

    def test_mark_in_progress(self, tmp_config, sample_features):
        tracker = ProgressTracker(tmp_config)
        progress = ProjectProgress(project_name="p", brief="b", features=sample_features)
        tracker.save(progress)
        progress = tracker.mark_in_progress(progress, "f1")
        assert progress.get_feature("f1").status == FeatureStatus.IN_PROGRESS
        assert progress.current_feature_id == "f1"

    def test_record_passing_evaluation(self, tmp_config, sample_features):
        tracker = ProgressTracker(tmp_config)
        progress = ProjectProgress(project_name="p", brief="b", features=sample_features)
        tracker.save(progress)
        progress = tracker.mark_in_progress(progress, "f1")

        result = EvaluationResult(
            design_quality=9,
            originality=9,
            craft=9,
            functionality=9,
            overall_score=9.0,
            feedback="Excellent",
            passed=True,
            iteration=1,
        )
        progress = tracker.record_evaluation(progress, "f1", result)
        f1 = progress.get_feature("f1")
        assert f1.status == FeatureStatus.PASSING
        assert f1.latest_evaluation.overall_score == 9.0
        assert progress.current_feature_id is None

    def test_markdown_summary_written(self, tmp_config, sample_features):
        tracker = ProgressTracker(tmp_config)
        progress = ProjectProgress(project_name="p", brief="b", features=sample_features)
        tracker.save(progress)
        assert tmp_config.progress_path.exists()
        content = tmp_config.progress_path.read_text()
        assert "User auth" in content
