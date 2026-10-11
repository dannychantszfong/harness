"""Subprocess Runner — invokes the `claude` CLI binary.

Billing:  your Claude subscription (Pro / Max)
File I/O: full — Claude Code writes files, runs commands, commits git
Best for: users who want to use their subscription and keep it simple

The runner shells out to `claude --print --output-format json`. The prompt
is piped through stdin (never argv: Linux rejects a single argument over
128 KiB, argv is visible in `ps`, and a prompt starting with `-` would be
parsed as a flag). Claude Code handles all tool use (Read, Write, Edit,
Bash) internally; the JSON result carries the final message, token usage
and cost, which feed context resets and cost tracking.
"""

import json
import subprocess
import shutil
from typing import Any, Optional

from rich.console import Console

from harness.runners._rate_limit import classify_failure, failure_haystack
from harness.runners.base import (
    CodeRunner,
    PreflightResult,
    RunResult,
    RunnerType,
    missing_cwd_result,
)
from harness.ui import QuietAnimator


console = Console()


def parse_claude_json(stdout: str) -> Optional[dict[str, Any]]:
    """Return the `result` object from `claude --output-format json`, or None.

    Tolerates log lines printed before the JSON and the array shape that
    `--verbose` produces (every message, the last one being the result).
    """
    text = (stdout or "").strip()
    if not text:
        return None
    candidates = [text] + text.splitlines()[::-1]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, list):
            results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
            data = results[-1] if results else None
        if isinstance(data, dict) and ("result" in data or data.get("type") == "result"):
            return data
    return None


def usage_from_claude_result(data: dict[str, Any]) -> dict[str, Optional[int]]:
    """Map a Claude Code `usage` dict onto RunResult token fields."""
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {
            k: getattr(usage, k, 0)
            for k in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        }
    if not usage:
        return {"input_tokens": None, "output_tokens": None, "cache_read_tokens": None}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
    }


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

    def _permission_args(self) -> list[str]:
        return ["--dangerously-skip-permissions"]  # required for unattended use

    def build_command(self) -> list[str]:
        cmd = [
            "claude",
            "--print",                 # non-interactive, print final output
            "--output-format", "json",  # final message + usage + cost
            *self._permission_args(),
        ]
        model = getattr(self.config, "code_runner_model", None)
        if model:
            cmd.extend(["--model", model])
        cmd.extend(getattr(self.config, "code_runner_extra_args", []) or [])
        return cmd

    def implement(
        self,
        prompt: str,
        cwd: str,
        timeout_seconds: int | None = None,
    ) -> RunResult:
        timeout_seconds = self._timeout(timeout_seconds)
        missing = missing_cwd_result(cwd)
        if missing is not None:
            return missing
        binary = shutil.which("claude")
        if not binary:
            return RunResult(
                output="",
                success=False,
                error=(
                    "`claude` binary not found on PATH. "
                    "Install Claude Code: https://claude.ai/download"
                ),
            )

        console.print("[dim]Runner: Claude Code CLI (subprocess) — using subscription[/dim]")

        # Launch the resolved path: on Windows `claude` may be a .cmd shim
        # that CreateProcess cannot find by bare name.
        cmd = [binary, *self.build_command()[1:]]
        try:
            with QuietAnimator.from_config(self.config, phase="coding"):
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    env=self.subprocess_env(),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            return RunResult(
                output="",
                success=False,
                error=f"Claude Code timed out after {timeout_seconds}s",
                timed_out=True,
            )
        except FileNotFoundError:
            return RunResult(
                output="",
                success=False,
                error="`claude` binary not found. Is Claude Code installed?",
            )
        except OSError as exc:
            return RunResult(
                output="",
                success=False,
                error=f"Could not start `claude`: {exc}",
            )

        data = parse_claude_json(result.stdout or "")
        is_error = bool(data and (data.get("is_error") or data.get("subtype", "success") != "success"))
        result_text = data.get("result") if data else None
        if not isinstance(result_text, str):
            result_text = None
        usage = usage_from_claude_result(data) if data else {}
        cost = data.get("total_cost_usd") if data else None
        session_id = data.get("session_id") if data else None

        if result.returncode != 0 or is_error:
            haystack = failure_haystack(
                None if data else result.stdout,
                result.stderr,
                result_text if is_error or result.returncode != 0 else None,
            )
            reset_at, rate_limited = classify_failure(haystack)
            stderr_tail = (result.stderr or "").strip()[-1500:]
            parts = [f"claude --print exited with code {result.returncode}"]
            if data is not None and is_error:
                parts.append(f"session ended with {data.get('subtype') or 'error'}")
            parts.append(f"prompt size: {len(prompt)} chars")
            if stderr_tail:
                parts.append(f"stderr (last 1500 chars):\n{stderr_tail}")
            else:
                parts.append("stderr: <empty>")
            detail = result_text if result_text is not None else (result.stdout or "").strip()
            if detail:
                parts.append(f"output (last 1500 chars):\n{detail[-1500:]}")
            return RunResult(
                output=result_text if result_text is not None else (result.stdout or ""),
                success=False,
                error="\n".join(parts),
                rate_limit_reset_at=reset_at,
                rate_limited=rate_limited,
                cost_usd=cost,
                session_id=session_id,
                **usage,
            )

        console.print("[dim]Claude Code exited cleanly.[/dim]")
        if data is None:
            # Older CLI or a wrapper that ignored --output-format: plain text,
            # no usage data.
            return RunResult(output=(result.stdout or "").strip(), success=True)

        return RunResult(
            output=(result_text or "").strip(),
            success=True,
            cost_usd=cost,
            session_id=session_id,
            **usage,
        )
