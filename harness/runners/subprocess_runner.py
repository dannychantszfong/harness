"""Subprocess Runner — invokes the `claude` CLI binary.

Billing:  your Claude subscription (Pro / Max)
File I/O: full — Claude Code writes files, runs commands, commits git
Best for: users who want to use their subscription and keep it simple

The runner shells out to `claude --print` with the feature prompt.
Claude Code handles all tool use (Read, Write, Edit, Bash) internally.
The final printed message is returned as the self-evaluation.
"""

import re
import subprocess
import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from rich.console import Console

from harness.runners.base import CodeRunner, PreflightResult, RunResult, RunnerType


# Pattern observed from `claude --print`:
#   "You've hit your limit · resets 9:30pm (Europe/London)"
# The hour is 1-12 with optional minutes, am/pm, and an IANA-style zone.
_RATE_LIMIT_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_RATE_LIMIT_TRIGGERS = ("you've hit your limit", "you have hit your limit")


def _parse_reset_time(text: str, now_utc: Optional[datetime] = None) -> Optional[datetime]:
    """Extract the next reset moment as a UTC datetime.

    Returns None if no recognizable rate-limit reset hint is in the text.
    The returned datetime is the *next* occurrence of the stated wall-clock
    time in the stated zone — today if it's still ahead, otherwise tomorrow.
    """
    if not any(trigger in text.lower() for trigger in _RATE_LIMIT_TRIGGERS):
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

console = Console()


class SubprocessRunner(CodeRunner):
    """Runs `claude --print` as a child process."""

    runner_type = RunnerType.SUBPROCESS

    def preflight(self) -> PreflightResult:
        path = shutil.which("claude")
        if not path:
            return PreflightResult(
                ok=False,
                summary="Claude Code CLI  ·  subscription billing  ·  full file I/O",
                details="",
                error=(
                    "`claude` binary not found on PATH.\n"
                    "Install Claude Code: https://claude.ai/download\n"
                    "Then re-run to continue."
                ),
            )
        # Try to get the version so we can show it
        try:
            ver = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            ver = "unknown version"
        model = getattr(self.config, "code_runner_model", None) or "runner default"
        return PreflightResult(
            ok=True,
            summary="Claude Code CLI  ·  subscription billing  ·  full file I/O",
            details=f"Binary: {path}  ({ver})   Model: {model}",
        )

    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        if not shutil.which("claude"):
            return RunResult(
                output="",
                success=False,
                error=(
                    "`claude` binary not found on PATH. "
                    "Install Claude Code: https://claude.ai/download"
                ),
            )

        console.print("[dim]Runner: Claude Code CLI (subprocess) — using subscription[/dim]")

        cmd = [
            "claude",
            "--print",                         # non-interactive, print final output
            "--dangerously-skip-permissions",  # required for unattended use
        ]
        model = getattr(self.config, "code_runner_model", None)
        if model:
            cmd.extend(["--model", model])
        cmd.extend(getattr(self.config, "code_runner_extra_args", []) or [])
        cmd.append(prompt)

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                output="",
                success=False,
                error=f"Claude Code timed out after {timeout_seconds}s",
            )
        except FileNotFoundError:
            return RunResult(
                output="",
                success=False,
                error="`claude` binary not found. Is Claude Code installed?",
            )

        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()[-1500:]
            stdout_tail = (result.stdout or "").strip()[-1500:]
            combined = f"{result.stdout or ''}\n{result.stderr or ''}"
            reset_at = _parse_reset_time(combined)
            parts = [f"claude --print exited with code {result.returncode}"]
            parts.append(f"prompt size: {len(prompt)} chars")
            if stderr_tail:
                parts.append(f"stderr (last 1500 chars):\n{stderr_tail}")
            else:
                parts.append("stderr: <empty>")
            if stdout_tail:
                parts.append(f"stdout (last 1500 chars):\n{stdout_tail}")
            return RunResult(
                output=result.stdout,
                success=False,
                error="\n".join(parts),
                rate_limit_reset_at=reset_at,
            )

        output = result.stdout.strip()
        console.print(f"[dim]Claude Code exited cleanly.[/dim]")

        # Subprocess gives no token/cost data — that's the trade-off vs SDK
        return RunResult(
            output=output,
            success=True,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
        )
