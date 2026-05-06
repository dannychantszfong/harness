"""Small terminal animation for quiet agent waits."""

from __future__ import annotations

import os
import sys
import threading
import time
from types import TracebackType
from typing import TextIO


_QUIET_FRAMES = (
    "✦",
    "✧",
    "✶",
    "✷",
    "✸",
    "✹",
    "✺",
    "✹",
    "✸",
    "✷",
    "✶",
    "✧",
)


class QuietSpinner:
    """Animate while a blocking operation is otherwise silent.

    The spinner writes to stderr and only runs on interactive terminals, so
    command output and test snapshots stay clean. Set HARNESS_NO_SPINNER=1 to
    disable it even in a TTY.
    """

    def __init__(
        self,
        message: str,
        *,
        frames: tuple[str, ...] = _QUIET_FRAMES,
        interval_seconds: float = 0.12,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.message = message
        self.frames = frames
        self.interval_seconds = interval_seconds
        self.stream = stream or sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_len = 0
        if enabled is None:
            disabled = os.environ.get("HARNESS_NO_SPINNER", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            enabled = bool(getattr(self.stream, "isatty", lambda: False)()) and not disabled
        self.enabled = enabled

    def __enter__(self) -> "QuietSpinner":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds * 2)
        self._clear()

    def _animate(self) -> None:
        idx = 0
        while not self._stop.is_set():
            frame = self.frames[idx % len(self.frames)]
            line = f"{frame} {self.message}"
            self._last_len = max(self._last_len, len(line))
            self.stream.write("\r" + line)
            self.stream.flush()
            idx += 1
            self._stop.wait(self.interval_seconds)

    def _clear(self) -> None:
        self.stream.write("\r" + (" " * self._last_len) + "\r")
        self.stream.flush()
