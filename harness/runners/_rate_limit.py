"""Shared rate-limit detection + reset-time parsing for all runners.

Two signals share this module:

  • `looks_rate_limited(text)` — broad heuristic. Used to flag a runner
    failure as rate-limit-class so the orchestrator rotates to the next
    profile in the role's chain. Cheap, conservative.

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
# a reset time is present. Used by sdk/codex runners to set the boolean
# flag and trigger profile rotation.
RATE_LIMIT_HINTS: tuple[str, ...] = _RESET_PARSE_TRIGGERS + (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "rate_limited",
    "usage limit",
    "usage cap",
    "quota",
    "too many requests",
    "429",
    "request was throttled",
)


def looks_rate_limited(text: str) -> bool:
    """True if the failure message smells like a rate/usage limit.

    Conservative on purpose: we only want this to fire when rotating
    providers is the right move. Bare 4xx errors that aren't 429 don't
    qualify.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in RATE_LIMIT_HINTS)


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
