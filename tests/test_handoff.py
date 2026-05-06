"""Tests for HandoffDocument and ContextReset."""

import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from harness.context.handoff import HandoffDocument
from harness.context.reset import ContextReset
from harness.config import HarnessConfig
from harness.progress.models import Feature, FeatureStatus, ProjectProgress


@pytest.fixture
def tmp_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="test-project",
        brief="A test project",
        output_dir=str(tmp_path),
        context_reset_threshold_tokens=1000,
    )


@pytest.fixture
def sample_progress() -> ProjectProgress:
    features = [
        Feature(id="f1", name="Auth", description="Login/signup", priority=0),
        Feature(id="f2", name="Dashboard", description="Main view", priority=1),
    ]
    features[0].status = FeatureStatus.PASSING
    return ProjectProgress(project_name="test", brief="test brief", features=features)


class TestHandoffDocument:
    def test_save_and_load_latest(self, tmp_path: Path):
        doc = HandoffDocument(
            project_name="proj",
            session_number=1,
            completed_at=datetime.utcnow(),
            what_was_done="Implemented auth",
            current_state="Auth passing, dashboard next",
            next_action="Implement dashboard",
            open_questions=["Should we use JWT?"],
            warnings=["Database migrations pending"],
        )
        doc.save(tmp_path)

        loaded = HandoffDocument.load_latest(tmp_path)
        assert loaded is not None
        assert loaded.session_number == 1
        assert "Implement dashboard" in loaded.next_action
        assert "JWT" in loaded.open_questions[0]

    def test_load_latest_returns_none_when_empty(self, tmp_path: Path):
        assert HandoffDocument.load_latest(tmp_path) is None

    def test_load_latest_returns_highest_session(self, tmp_path: Path):
        for i in range(1, 4):
            doc = HandoffDocument(
                project_name="proj",
                session_number=i,
                completed_at=datetime.utcnow(),
                what_was_done=f"Session {i}",
                current_state="running",
                next_action="continue",
            )
            doc.save(tmp_path)

        latest = HandoffDocument.load_latest(tmp_path)
        assert latest.session_number == 3

    def test_to_prompt_block_contains_key_sections(self):
        doc = HandoffDocument(
            project_name="proj",
            session_number=2,
            completed_at=datetime.utcnow(),
            what_was_done="Did stuff",
            current_state="Good state",
            next_action="Do more stuff",
            open_questions=["Q1"],
            warnings=["W1"],
        )
        block = doc.to_prompt_block()
        assert "Session 2" in block
        assert "Did stuff" in block
        assert "Do more stuff" in block
        assert "Q1" in block
        assert "W1" in block


class TestContextReset:
    def test_should_reset_true_above_threshold(self, tmp_config):
        reset = ContextReset(tmp_config)
        assert reset.should_reset(1001) is True

    def test_should_reset_false_below_threshold(self, tmp_config):
        reset = ContextReset(tmp_config)
        assert reset.should_reset(999) is False

    def test_build_handoff_writes_file(self, tmp_config, sample_progress):
        reset = ContextReset(tmp_config)
        handoff = reset.build_handoff(
            progress=sample_progress,
            session_number=2,
            what_was_done="Worked on dashboard",
            current_state="Dashboard failing",
            next_action="Fix dashboard",
        )
        output_dir = Path(tmp_config.output_dir)
        saved = list(output_dir.glob("handoff_session_*.json"))
        assert len(saved) == 1
        assert handoff.session_number == 2

    def test_format_session_preamble_with_handoff(self, tmp_config, sample_progress):
        reset = ContextReset(tmp_config)
        handoff = HandoffDocument(
            project_name="test",
            session_number=3,
            completed_at=datetime.utcnow(),
            what_was_done="previous work",
            current_state="state",
            next_action="next thing",
        )
        preamble = ContextReset.format_session_preamble(handoff, sample_progress)
        assert "Session 3" in preamble
        assert "next thing" in preamble

    def test_format_session_preamble_no_handoff(self, sample_progress):
        preamble = ContextReset.format_session_preamble(None, sample_progress)
        assert "First session" in preamble
