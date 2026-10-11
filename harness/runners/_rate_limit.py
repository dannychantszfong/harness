"""Shared rate-limit detection + reset-time parsing for all runners.

Two signals share this module:

  • `looks_rate_limited(text)` — broad heuristic. Used to flag a runner
    failure as rate-limit-class so the orchestrator rotates to the next
    profile in the role's chain. Cheap, conservative; feed it
    `failure_haystack(stdout, stderr)` rather than raw output.

  • `parse_reset_time(text)` — strict parser for the "You've hit your
    limit · resets 9:30pm (Europe/London)" pattern Claude Code prints
    when a Pro/Max subscription cap is hit. Returns a tz-aware UTC
    datetime, or None when the message doesn't carry a parseable hint.
    A None result is normal for SDK and Codex which surface the cap
    via shorter exception text.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


# Pattern observed from `claude --print`:
#   "You've hit your limit · resets 9:30pm (Europe/London)"
_RATE_LIMIT_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
    re.IGNORECASE,
)

# "Strict" triggers — phrases that gate the reset-time parse so we don't
# mistakenly read "resets at 9pm" out of unrelated text.
_RESET_PARSE_TRIGGERS: tuple[str, ...] = (
    "you've hit your limit",
    "you have hit your limit",
)

# "Broad" hints — anything that smells rate-limited regardless of whether
# a reset time is present. Used to set the boolean flag and trigger profile
# rotation. Word boundaries keep "429" from matching inside port numbers or
# ids, and "quota" only counts next to "exceeded"/"limit" (ADR-011:
# rotation must stay conservative).
RATE_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"you(?:'ve| have) hit your (?:usage )?limit",
        # rate limit / rate-limited / rate_limit_error, but not "RateLimiter"
        r"\brate[\s_-]?limit(?:ed|s|_error|_exceeded)?\b",
        r"\busage (?:limit|cap)\b",
        r"\bquota\b.{0,40}\b(?:exceeded|limit)",
        r"\b(?:exceeded|limit)\b.{0,40}\bquota\b",
        r"\btoo many requests\b",
        r"\b429\b",
        r"\brequest was throttled\b",
    )
)

# How much of stdout is considered. Agents print their whole transcript to
# stdout; only the tail is where a CLI reports why it stopped.
STDOUT_TAIL_LINES = 20


def failure_haystack(stdout: str | None, stderr: str | None, *extra: str | None) -> str:
    """Text a failed run is classified on: extra messages + stderr + stdout tail.

    `extra` carries structured error text the runner already extracted
    (e.g. a JSON `result` with is_error=true), which is the most precise
    signal and therefore goes first.
    """
    tail = "\n".join((stdout or "").strip().splitlines()[-STDOUT_TAIL_LINES:])
    parts = [*(e for e in extra if e), stderr or "", tail]
    return "\n".join(p for p in parts if p)


def looks_rate_limited(text: str) -> bool:
    """True if the failure message smells like a rate/usage limit.

    Conservative on purpose: we only want this to fire when rotating
    providers is the right move. Bare 4xx errors that aren't 429 don't
    qualify. Callers should pass `failure_haystack(...)`, not a whole
    transcript — a generator implementing a rate limiter must not look
    capped.
    """
    if not text:
        return False
    return any(p.search(text) for p in RATE_LIMIT_PATTERNS)


def classify_failure(text: str) -> tuple[Optional[datetime], bool]:
    """(reset_at, rate_limited) for a failed run's haystack."""
    reset_at = parse_reset_time(text)
    return reset_at, reset_at is not None or looks_rate_limited(text)


def parse_reset_time(
    text: str,
    *,
    now_utc: Optional[datetime] = None,
) -> Optional[datetime]:
    """Extract the next reset moment as a UTC datetime, or None.

    Returns None when the text doesn't contain the strict
    "You've hit your limit · resets <time> (<tz>)" pattern. SDK/Codex
    rate-limit messages typically don't include a reset time and will
    return None — that's expected.

    The returned datetime is the *next* occurrence of the stated
    wall-clock time in the stated zone — today if still ahead,
    otherwise tomorrow.
    """
    if not text:
        return None
    lowered = text.lower()
    if not any(trigger in lowered for trigger in _RESET_PARSE_TRIGGERS):
        return None
    m = _RATE_LIMIT_RESET_RE.search(text)
    if not m:
        return None
    hour_12 = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    meridiem = m.group(3).lower()
    tz_name = m.group(4).strip()

    hour_24 = hour_12 % 12 + (12 if meridiem == "pm" else 0)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return None

    now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    candidate = now.replace(hour=hour_24, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)
