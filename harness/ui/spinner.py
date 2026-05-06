"""Terminal animation for quiet agent waits."""

from __future__ import annotations

import os
import sys
import threading
from types import TracebackType
from typing import Any, TextIO


FRAME_PACKS: dict[str, tuple[str, ...]] = {
    "sparkle": ("✧", "✦", "✨", "✦", "✧", "✶", "✷", "✸", "✹", "✺", "✹", "✸", "✷", "✶"),
    "bloom": ("·", "❀", "❁", "✿", "❁", "❀", "·"),
    "snow": ("❄", "✼", "❅", "✾", "❆", "✽"),
    "braille": ("⠁", "⠂", "⠄", "⡀", "⢀", "⠠", "⠐", "⠈"),
    "orbit": ("◌", "○", "◎", "●", "◎", "○"),
    "pulse": ("·", "○", "◉", "●", "◉", "○", "·"),
    "dots": ("⠁", "⠂", "⠄", "⠂"),
    "moon": ("◐", "◓", "◑", "◒"),
    "bars": ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂"),
    "clock": ("◴", "◷", "◶", "◵"),
    "wave": ("~", "∿", "≈", "∿"),
    "tech": ("░", "▒", "▓", "█", "▓", "▒"),
}

SCRAMBLE_CHARS = ("✧", "✦", "·", "⋆", "░", "▒", "/", "\\", "|", "-")

PHRASES: dict[str, dict[str, tuple[str, ...]]] = {
    "steady": {
        "planning": ("Planning", "Mapping", "Shaping"),
        "coding": ("Working", "Applying", "Building"),
        "evaluating": ("Evaluating", "Checking", "Reviewing"),
        "waiting": ("Thinking", "Working", "Waiting"),
    },
    "playful": {
        "planning": ("Scrying", "Charting", "Divining", "Attuning", "Revealing"),
        "coding": (
            "Inscribing",
            "Enchanting",
            "Forging",
            "Weaving",
            "Transmuting",
            "Binding",
            "Warding",
        ),
        "evaluating": ("Revealing", "Discerning", "Scrying", "Weighing", "Testing"),
        "waiting": ("Channeling", "Attuning", "Scrying", "Conjuring", "Gathering"),
    },
}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def _pack(name: str | None) -> tuple[str, ...]:
    return FRAME_PACKS.get(name or "", FRAME_PACKS["sparkle"])


def _phrase_pool(style: str, phase: str) -> tuple[str, ...]:
    style_map = PHRASES.get(style, PHRASES["playful"])
    return style_map.get(phase, style_map["waiting"])


class QuietAnimator:
    """Animate while a blocking operation is otherwise silent.

    The animator writes to stderr and only runs on interactive terminals, so
    command output and test snapshots stay clean. Set HARNESS_NO_SPINNER=1 to
    disable it even in a TTY.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        phase: str = "waiting",
        subject: str | None = None,
        frame_pack: str = "sparkle",
        frames: tuple[str, ...] | None = None,
        phrase_style: str = "playful",
        text_effect: str = "typewriter",
        interval_seconds: float = 0.12,
        phrase_interval_ticks: int = 28,
        transition_ticks: int = 8,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.message = message
        self.phase = phase
        self.subject = subject
        self.frames = frames or _pack(frame_pack)
        self.phrases = (message,) if message else _phrase_pool(phrase_style, phase)
        self.text_effect = text_effect
        self.interval_seconds = interval_seconds
        self.phrase_interval_ticks = max(1, phrase_interval_ticks)
        self.transition_ticks = max(1, transition_ticks)
        self.stream = stream or sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_len = 0
        if enabled is None:
            enabled = bool(getattr(self.stream, "isatty", lambda: False)()) and not _truthy_env(
                "HARNESS_NO_SPINNER"
            )
        self.enabled = enabled

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        phase: str,
        subject: str | None = None,
        message: str | None = None,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> "QuietAnimator":
        return cls(
            message=message,
            phase=phase,
            subject=subject,
            frame_pack=getattr(config, "progress_animation", "sparkle"),
            phrase_style=getattr(config, "progress_phrase_style", "playful"),
            text_effect=getattr(config, "progress_text_effect", "typewriter"),
            stream=stream,
            enabled=enabled,
        )

    def __enter__(self) -> "QuietAnimator":
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

    def render_line(self, tick: int) -> str:
        frame = self.frames[tick % len(self.frames)]
        phrase_index = (tick // self.phrase_interval_ticks) % len(self.phrases)
        phrase_tick = tick % self.phrase_interval_ticks
        phrase = self._render_phrase(self.phrases[phrase_index], phrase_tick)
        if self.subject:
            phrase = f"{phrase} with {self.subject}"
        return f"{frame} {phrase}"

    def _render_phrase(self, phrase: str, tick: int) -> str:
        if self.text_effect == "none" or tick >= self.transition_ticks:
            return phrase
        if self.text_effect == "scramble":
            return self._scramble(phrase, tick)
        # Default: typewriter reveal.
        reveal = max(1, int(len(phrase) * (tick + 1) / self.transition_ticks))
        return phrase[:reveal]

    def _scramble(self, phrase: str, tick: int) -> str:
        reveal = int(len(phrase) * tick / self.transition_ticks)
        chars: list[str] = []
        for idx, char in enumerate(phrase):
            if char.isspace() or idx < reveal:
                chars.append(char)
            else:
                chars.append(SCRAMBLE_CHARS[(tick + idx) % len(SCRAMBLE_CHARS)])
        return "".join(chars)

    def _animate(self) -> None:
        tick = 0
        while not self._stop.is_set():
            line = self.render_line(tick)
            self._last_len = max(self._last_len, len(line))
            self.stream.write("\r" + line)
            self.stream.flush()
            tick += 1
            self._stop.wait(self.interval_seconds)

    def _clear(self) -> None:
        self.stream.write("\r" + (" " * self._last_len) + "\r")
        self.stream.flush()


class QuietSpinner(QuietAnimator):
    """Compatibility wrapper for the original fixed-message spinner."""

    def __init__(
        self,
        message: str,
        *,
        frames: tuple[str, ...] = FRAME_PACKS["sparkle"],
        interval_seconds: float = 0.12,
        stream: TextIO | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(
            message=message,
            frames=frames,
            text_effect="none",
            interval_seconds=interval_seconds,
            stream=stream,
            enabled=enabled,
        )
