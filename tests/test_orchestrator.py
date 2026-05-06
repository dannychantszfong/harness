"""Integration-level tests for the Orchestrator.

These tests mock the Anthropic client to avoid real API calls.
They verify the orchestration logic: phase ordering, context reset triggers,
GAN loop iteration, and progress file updates.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from harness.config import CONFIG_FILENAME, HarnessConfig, RunnerProfile
from harness.orchestrator import Orchestrator
from harness.progress.models import (
    Feature,
    FeatureStatus,
    ProjectProgress,
    EvaluationResult,
    SprintContract,
)
from harness.progress.tracker import ProgressTracker
from harness.runners.base import PreflightResult, RunResult, RunnerType


def _make_sprint_contract(feature_id: str = "f1") -> SprintContract:
    return SprintContract(
        feature_id=feature_id,
        acceptance_criteria=["It works"],
        out_of_scope=["Nothing yet"],
    )


@pytest.fixture
def tmp_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="test-project",
        brief="A simple test project",
        output_dir=str(tmp_path),
        max_iterations_per_feature=3,
        evaluator_pass_score=8.0,
        context_reset_threshold_tokens=999_999,  # prevent resets in unit tests
    )


@pytest.fixture
def one_feature_progress(tmp_path: Path, tmp_config: HarnessConfig) -> ProjectProgress:
    features = [Feature(id="f1", name="Auth", description="Login/signup", priority=0)]
    progress = ProjectProgress(
        project_name="test-project",
        brief="A simple test project",
        features=features,
    )
    tracker = ProgressTracker(tmp_config)
    tracker.save(progress)
    return progress


def _make_passing_eval(iteration: int = 1) -> EvaluationResult:
    return EvaluationResult(
        design_quality=9,
        originality=9,
        craft=9,
        functionality=9,
        overall_score=9.0,
        feedback="Great work",
        passed=True,
        iteration=iteration,
    )


def _make_failing_eval(iteration: int = 1) -> EvaluationResult:
    return EvaluationResult(
        design_quality=5,
        originality=5,
        craft=5,
        functionality=5,
        overall_score=5.0,
        feedback="Needs improvement",
        passed=False,
        iteration=iteration,
    )


class TestOrchestratorPhases:
    def test_feature_passes_on_first_iteration(
        self, tmp_config, one_feature_progress
    ):
        orchestrator = Orchestrator(tmp_config)

        with (
            patch.object(orchestrator, "_initialize", return_value=one_feature_progress),
            patch.object(orchestrator, "_plan", return_value=one_feature_progress),
            patch("harness.orchestrator.GeneratorAgent") as MockGen,
            patch("harness.orchestrator.EvaluatorAgent") as MockEval,
            patch("harness.orchestrator.sync_project_git") as mock_sync,
        ):
            mock_sync.return_value = MagicMock(ok=True, skipped=False, message="pushed")
            mock_gen = MockGen.return_value
            mock_gen.implement_feature.return_value = "self eval text"
            mock_gen.negotiate_sprint_contract.return_value = _make_sprint_contract()
            mock_gen.usage = MagicMock(total_tokens=100)

            mock_eval = MockEval.return_value
            mock_eval.evaluate.return_value = _make_passing_eval()
            mock_eval.usage = MagicMock(total_tokens=100)

            orchestrator.run()

            tracker = ProgressTracker(tmp_config)
            progress = tracker.load()
            f1 = progress.get_feature("f1")
            assert f1.status == FeatureStatus.PASSING
            assert mock_sync.call_count == 2

    def test_feature_iterates_until_pass(self, tmp_config, one_feature_progress):
        orchestrator = Orchestrator(tmp_config)

        with (
            patch.object(orchestrator, "_initialize", return_value=one_feature_progress),
            patch.object(orchestrator, "_plan", return_value=one_feature_progress),
            patch("harness.orchestrator.GeneratorAgent") as MockGen,
            patch("harness.orchestrator.EvaluatorAgent") as MockEval,
        ):
            mock_gen = MockGen.return_value
            mock_gen.implement_feature.return_value = "self eval"
            mock_gen.negotiate_sprint_contract.return_value = _make_sprint_contract()
            mock_gen.usage = MagicMock(total_tokens=50)

            mock_eval = MockEval.return_value
            # Fail twice, then pass
            mock_eval.evaluate.side_effect = [
                _make_failing_eval(1),
                _make_failing_eval(2),
                _make_passing_eval(3),
            ]
            mock_eval.usage = MagicMock(total_tokens=50)

            orchestrator.run()

            assert mock_gen.implement_feature.call_count == 3
            assert mock_eval.evaluate.call_count == 3

    def test_max_iterations_respected(self, tmp_config, one_feature_progress):
        tmp_config.max_iterations_per_feature = 2
        orchestrator = Orchestrator(tmp_config)

        with (
            patch.object(orchestrator, "_initialize", return_value=one_feature_progress),
            patch.object(orchestrator, "_plan", return_value=one_feature_progress),
            patch("harness.orchestrator.GeneratorAgent") as MockGen,
            patch("harness.orchestrator.EvaluatorAgent") as MockEval,
        ):
            mock_gen = MockGen.return_value
            mock_gen.implement_feature.return_value = "self eval"
            mock_gen.negotiate_sprint_contract.return_value = _make_sprint_contract()
            mock_gen.usage = MagicMock(total_tokens=50)

            mock_eval = MockEval.return_value
            mock_eval.evaluate.return_value = _make_failing_eval()
            mock_eval.usage = MagicMock(total_tokens=50)

            orchestrator.run()

            # Should stop after max_iterations even if never passing
            assert mock_gen.implement_feature.call_count == 2

    def test_generator_rate_limit_rotates_to_next_profile(
        self, tmp_config, one_feature_progress, monkeypatch
    ):
        tmp_config.orchestration_mode = "runner"
        tmp_config.code_runner = "subprocess"
        tmp_config.runner_profiles = [
            RunnerProfile(name="claude", runner="subprocess", model="sonnet"),
            RunnerProfile(name="codex", runner="codex", model="gpt-5.2"),
        ]
        tmp_config.generator_runner_order = ["claude", "codex"]
        tmp_config.evaluator_runner_order = ["codex"]

        created = {}

        def fake_create_runner(runner_type, cfg):
            runner = MagicMock()
            runner.runner_type = runner_type
            runner.preflight.return_value = PreflightResult(ok=True, summary="ok", details="ok")
            key = cfg.code_runner_model or "legacy"
            if key == "sonnet":
                runner.implement.return_value = RunResult(
                    output="",
                    success=False,
                    error="usage cap",
                    rate_limited=True,
                )
            else:
                runner.implement.return_value = RunResult(
                    output="self eval",
                    success=True,
                )
            created.setdefault(key, []).append(runner)
            return runner

        monkeypatch.setattr("harness.orchestrator.create_runner", fake_create_runner)
        monkeypatch.setattr("harness.runner_profiles.create_runner", fake_create_runner)

        orchestrator = Orchestrator(tmp_config, runner_type=RunnerType.SUBPROCESS)

        with (
            patch.object(orchestrator, "_initialize", return_value=one_feature_progress),
            patch.object(orchestrator, "_plan", return_value=one_feature_progress),
            patch("harness.orchestrator.EvaluatorAgent") as MockEval,
        ):
            mock_eval = MockEval.return_value
            mock_eval.evaluate.return_value = _make_passing_eval()
            mock_eval.usage = MagicMock(total_tokens=50)

            orchestrator.run()

        assert created["sonnet"][0].implement.call_count == 1
        assert any(r.implement.call_count >= 1 for r in created["gpt-5.2"])


# ── Live config reload at seams ──────────────────────────────────────────────

class TestLiveConfigReload:
    """The orchestrator re-reads harness_config.json at every natural seam (between
    features, between GAN iterations) so users can swap models / weights /
    iteration caps mid-run by editing the file."""

    def test_reload_picks_up_model_changes(self, tmp_path, monkeypatch):
        from harness.config import HarnessConfig
        from harness.orchestrator import Orchestrator
        # Skip the runner preflight banner and avoid creating a real runner
        monkeypatch.setattr(Orchestrator, "_print_runner_status", lambda self: None)
        monkeypatch.setattr(
            "harness.orchestrator.create_runner", lambda *a, **k: MagicMock()
        )

        config = HarnessConfig(
            project_name="t", brief="b",
            output_dir=str(tmp_path),
            orchestration_mode="runner",
            code_runner="subprocess",
            generator_model="claude-opus-4-7",
            evaluator_pass_score=8.0,
        )
        config_path = tmp_path / CONFIG_FILENAME
        config.save_yaml(config_path)

        orch = Orchestrator(config)

        # Write a *different* config to disk via a fresh object so we don't
        # accidentally mutate orch.config in memory (same reference!).
        edited = HarnessConfig(
            project_name="t", brief="b",
            output_dir=str(tmp_path),
            orchestration_mode="runner",
            code_runner="subprocess",
            generator_model="claude-sonnet-4-6",
            evaluator_pass_score=7.0,
            project_id=config.project_id,
        )
        edited.save_yaml(config_path)
        # Force mtime forward (writes within the same second can otherwise
        # share a timestamp on coarse filesystems).
        import os, time
        future = time.time() + 10
        os.utime(config_path, (future, future))

        changes = orch._reload_config_if_changed()
        assert "generator_model" in changes
        assert "evaluator_pass_score" in changes
        assert orch.config.generator_model == "claude-sonnet-4-6"
        assert orch.config.evaluator_pass_score == 7.0

    def test_reload_returns_empty_when_unchanged(self, tmp_path, monkeypatch):
        from harness.config import HarnessConfig
        from harness.orchestrator import Orchestrator
        monkeypatch.setattr(Orchestrator, "_print_runner_status", lambda self: None)
        monkeypatch.setattr(
            "harness.orchestrator.create_runner", lambda *a, **k: MagicMock()
        )

        config = HarnessConfig(
            project_name="t", brief="b",
            output_dir=str(tmp_path),
            orchestration_mode="runner",
            code_runner="subprocess",
        )
        config.save_yaml(tmp_path / CONFIG_FILENAME)
        orch = Orchestrator(config)

        # Two consecutive reloads with no file change → both no-ops
        assert orch._reload_config_if_changed() == {}
        assert orch._reload_config_if_changed() == {}

    def test_reload_pins_identity_fields(self, tmp_path, monkeypatch):
        """Even if someone edits project_id / output_dir / code_runner in the
        file, the orchestrator must NOT pick those up — they define identity."""
        from harness.config import HarnessConfig
        from harness.orchestrator import Orchestrator
        monkeypatch.setattr(Orchestrator, "_print_runner_status", lambda self: None)
        monkeypatch.setattr(
            "harness.orchestrator.create_runner", lambda *a, **k: MagicMock()
        )

        config = HarnessConfig(
            project_name="t", brief="b",
            output_dir=str(tmp_path),
            orchestration_mode="runner",
            code_runner="subprocess",
            project_id="abc12345",
        )
        config_path = tmp_path / CONFIG_FILENAME
        config.save_yaml(config_path)
        orch = Orchestrator(config)

        # Tamper with identity fields on disk via a fresh object — same
        # reference trap as the previous test.
        tampered = HarnessConfig(
            project_name="t", brief="b",
            output_dir=str(tmp_path),
            orchestration_mode="runner",
            code_runner="codex",       # tampered
            project_id="deadbeef",      # tampered
        )
        tampered.save_yaml(config_path)
        import os, time
        future = time.time() + 10
        os.utime(config_path, (future, future))

        orch._reload_config_if_changed()
        # Identity fields stay pinned to startup values
        assert orch.config.project_id == "abc12345"
        assert orch.config.code_runner == "subprocess"

    def test_legacy_resume_writes_placeholder_spec_not_replanner(
        self, tmp_path, monkeypatch
    ):
        """A project with features.json but no spec.md must NOT re-run the
        planner (that would discard the prior alignment work). Instead, write
        a placeholder spec.md from the brief; user can edit it later."""
        from harness.config import HarnessConfig
        from harness.orchestrator import Orchestrator
        from harness.progress.models import Feature, ProjectProgress
        from harness.progress.tracker import ProgressTracker

        monkeypatch.setattr(Orchestrator, "_print_runner_status", lambda self: None)
        monkeypatch.setattr(
            "harness.orchestrator.create_runner", lambda *a, **k: MagicMock()
        )

        config = HarnessConfig(
            project_name="MicroWins",
            brief="A tiny daily progress tracker.",
            output_dir=str(tmp_path),
            orchestration_mode="runner",
            code_runner="subprocess",
        )
        config.save_yaml(tmp_path / CONFIG_FILENAME)

        # Pre-populate canonical features.json with several features but NO spec
        progress = ProjectProgress(
            project_name=config.project_name,
            brief=config.brief,
            features=[
                Feature(id=f"f{i}", name=f"F{i}", description=f"f{i}", priority=i)
                for i in range(5)
            ],
            spec=None,
        )
        ProgressTracker(config).save(progress)

        orch = Orchestrator(config)
        # If the planner agent gets called, this test should fail.
        with patch("harness.orchestrator.PlannerAgent") as MockPlanner:
            result = orch._plan(progress, confirmed_spec=None)
            MockPlanner.assert_not_called()

        # Spec.md was written, progress.spec set to it
        assert config.spec_path.exists()
        spec_text = config.spec_path.read_text()
        assert "Placeholder Spec" in spec_text
        assert config.brief in spec_text
        assert result.spec == spec_text

    def test_reload_called_at_feature_seam(self, tmp_config, one_feature_progress):
        """_feature_loop calls _reload_config_if_changed at least once per feature."""
        from harness.orchestrator import Orchestrator
        orchestrator = Orchestrator(tmp_config)

        with (
            patch.object(orchestrator, "_initialize", return_value=one_feature_progress),
            patch.object(orchestrator, "_plan", return_value=one_feature_progress),
            patch.object(
                orchestrator, "_reload_config_if_changed", wraps=orchestrator._reload_config_if_changed
            ) as spy,
            patch("harness.orchestrator.GeneratorAgent") as MockGen,
            patch("harness.orchestrator.EvaluatorAgent") as MockEval,
        ):
            mock_gen = MockGen.return_value
            mock_gen.implement_feature.return_value = "self eval"
            mock_gen.negotiate_sprint_contract.return_value = _make_sprint_contract()
            mock_gen.usage = MagicMock(total_tokens=10)

            mock_eval = MockEval.return_value
            mock_eval.evaluate.return_value = _make_passing_eval()
            mock_eval.usage = MagicMock(total_tokens=10)

            orchestrator.run()

            # Called at top of feature loop (1) + top of single iteration (1) +
            # one terminal pass when next_pending_feature returns None (1) = 3.
            # The exact count is less important than "got called more than zero
            # times across both seams".
            assert spy.call_count >= 2
