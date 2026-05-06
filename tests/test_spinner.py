"""Tests for terminal quiet-progress animation."""

import io

from harness.config import HarnessConfig
from harness.ui.spinner import FRAME_PACKS, QuietAnimator, QuietSpinner


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_spinner_disabled_on_non_tty():
    stream = io.StringIO()
    with QuietSpinner("working", stream=stream):
        pass
    assert stream.getvalue() == ""


def test_spinner_can_write_and_clear_when_enabled():
    stream = _TTYBuffer()
    spinner = QuietSpinner(
        "working",
        frames=("✦",),
        interval_seconds=0.001,
        stream=stream,
        enabled=True,
    )
    spinner.start()
    spinner.stop()
    text = stream.getvalue()
    assert "✦ working" in text
    assert text.endswith("\r")


def test_animator_has_named_frame_packs():
    assert "sparkle" in FRAME_PACKS
    assert "braille" in FRAME_PACKS
    assert "orbit" in FRAME_PACKS


def test_animator_uses_typewriter_effect():
    animator = QuietAnimator(
        phase="coding",
        frame_pack="sparkle",
        phrase_style="playful",
        text_effect="typewriter",
        frames=("✦",),
        enabled=False,
    )
    first = animator.render_line(0)
    later = animator.render_line(8)
    assert first.startswith("✦ ")
    assert "with" not in first
    assert "Inscribing" in later
    assert len(first) < len(later)


def test_animator_uses_scramble_effect():
    animator = QuietAnimator(
        message="Working",
        frames=("✧",),
        text_effect="scramble",
        enabled=False,
    )
    early = animator.render_line(0)
    late = animator.render_line(8)
    assert early != "✧ Working"
    assert late == "✧ Working"


def test_animator_reads_config():
    config = HarnessConfig(
        project_name="t",
        brief="b",
        progress_animation="orbit",
        progress_phrase_style="steady",
        progress_text_effect="none",
    )
    animator = QuietAnimator.from_config(
        config,
        phase="evaluating",
        enabled=False,
    )
    assert animator.frames == FRAME_PACKS["orbit"]
    assert animator.render_line(0) == "◌ Evaluating"


def test_playful_phrases_are_single_verbs():
    animator = QuietAnimator(
        phase="coding",
        phrase_style="playful",
        text_effect="none",
        enabled=False,
    )
    for phrase in animator.phrases:
        assert " " not in phrase
