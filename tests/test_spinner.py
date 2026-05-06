"""Tests for terminal quiet-progress animation."""

import io

from harness.ui.spinner import QuietSpinner


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
