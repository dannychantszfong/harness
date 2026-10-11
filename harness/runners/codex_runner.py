"""Codex Runner — invokes the OpenAI Codex CLI (`codex` binary).

Billing:  your OpenAI subscription / credits attached to the Codex CLI
File I/O: full — Codex writes files, runs commands, same model as CLI
Best for: users already on the OpenAI ecosystem

Install:  npm install -g @openai/codex   (or via the OpenAI app)
Docs:     https://github.com/openai/codex

The prompt is piped through stdin (`codex exec ... -`). `--json` turns
stdout into a JSONL event stream (token usage, errors) and
`--output-last-message` writes the agent's final message to a file, which
becomes the run's output. Verified against codex-cli 0.160.
"""

import json
import os
import subprocess
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console

from harness.runners import base as runner_base
from harness.runners._rate_limit import classify_failure, failure_haystack
from harness.runners.base import (
    CodeRunner,
    PreflightResult,
    RunResult,
    RunnerAccess,
    RunnerType,
    missing_cwd_result,
)
from harness.ui import QuietAnimator

console = Console()


@dataclass
class CodexEvents:
    """What the harness needs from a `codex exec --json` event stream."""
    parsed: bool = False
    last_message: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    saw_usage: bool = False
    thread_id: Optional[str] = None


def parse_codex_events(stdout: str) -> CodexEvents:
    events = CodexEvents()
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        events.parsed = True
        kind = event.get("type")
        if kind == "thread.started":
            events.thread_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                events.last_message = item["text"]
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            events.saw_usage = True
            events.input_tokens += int(usage.get("input_tokens") or 0)
            events.cached_input_tokens += int(usage.get("cached_input_tokens") or 0)
            events.output_tokens += int(usage.get("output_tokens") or 0)
        elif kind == "turn.failed":
            error = event.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            if message:
                events.errors.append(message)
        elif kind == "error" and event.get("message"):
            events.errors.append(str(event["message"]))
    return events


class CodexRunner(CodeRunner):
    """Runs `codex` CLI as a child process."""

    runner_type = RunnerType.CODEX

    def preflight(self) -> PreflightResult:
        path = shutil.which("codex")
        if not path:
            return PreflightResult(
                ok=False,
                summary="OpenAI Codex CLI  ·  OpenAI subscription  ·  full file I/O",
                details="",
                error=(
                    "`codex` binary not found on PATH.\n"
                    "Install: npm install -g @openai/codex"
                ),
            )
        try:
            ver = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            ver = "unknown version"
        model = getattr(self.config, "code_runner_model", None) or "runner default"
        provider = "default provider"
        local_provider = getattr(self.config, "codex_local_provider", None)
        if local_provider:
            provider = f"local OSS via {local_provider}"
        elif getattr(self.config, "codex_oss", False):
            provider = "open-source provider"
        return PreflightResult(
            ok=True,
            summary="OpenAI Codex CLI  ·  OpenAI subscription  ·  full file I/O",
            details=f"Binary: {path}  ({ver})   Model: {model}   Provider: {provider}",
        )

    @staticmethod
    def _permission_args(access: Optional[RunnerAccess] = None) -> list[str]:
        if access is not None and access.read_only:
            # Codex has no per-tool allow-list; the sandbox is the boundary.
            return ["--sandbox", "read-only"]
        return ["--dangerously-bypass-approvals-and-sandbox"]

    def build_command(
        self,
        last_message_path: str,
        access: Optional[RunnerAccess] = None,
    ) -> list[str]:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--output-last-message",
            last_message_path,
            *self._permission_args(access),
            "--skip-git-repo-check",
            "--color",
            "never",
        ]
        model = getattr(self.config, "code_runner_model", None)
        if model:
            cmd.extend(["--model", model])
        local_provider = getattr(self.config, "codex_local_provider", None)
        if local_provider:
            cmd.extend(["--oss", "--local-provider", local_provider])
        elif getattr(self.config, "codex_oss", False):
            cmd.append("--oss")
        cmd.extend(getattr(self.config, "code_runner_extra_args", []) or [])
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def implement(
        self,
        prompt: str,
        cwd: str,
        timeout_seconds: int | None = None,
        access: Optional[RunnerAccess] = None,
    ) -> RunResult:
        timeout_seconds = self._timeout(timeout_seconds)
        missing = missing_cwd_result(cwd)
        if missing is not None:
            return missing
        binary = shutil.which("codex")
        if not binary:
            return RunResult(
                output="",
                success=False,
                error=(
                    "`codex` binary not found on PATH. "
                    "Install: npm install -g @openai/codex  "
                    "or download from https://github.com/openai/codex"
                ),
            )

        console.print("[dim]Runner: OpenAI Codex CLI — using OpenAI subscription[/dim]")

        # Outside the project dir so it never lands in the project's git repo.
        fd, last_message_path = tempfile.mkstemp(prefix="harness-codex-", suffix=".txt")
        os.close(fd)
        try:
            # Resolved path: npm installs `codex` as a .cmd shim on Windows.
            cmd = [binary, *self.build_command(last_message_path, access)[1:]]
            try:
                with QuietAnimator.from_config(self.config, phase="coding"):
                    # Kills the whole process tree on timeout (npm shims on Windows).
                    result = runner_base.run_process(
                        cmd,
                        cwd=cwd,
                        env=self.subprocess_env(),
                        input=prompt,
                        timeout=timeout_seconds,
                    )
            except subprocess.TimeoutExpired:
                return RunResult(
                    output="",
                    success=False,
                    error=f"Codex timed out after {timeout_seconds}s",
                    timed_out=True,
                )
            except FileNotFoundError:
                return RunResult(
                    output="",
                    success=False,
                    error="`codex` binary not found. Is the Codex CLI installed?",
                )
            except OSError as exc:
                return RunResult(
                    output="",
                    success=False,
                    error=f"Could not start `codex`: {exc}",
                )
            try:
                last_message = Path(last_message_path).read_text(encoding="utf-8").strip()
            except OSError:
                last_message = ""
        finally:
            try:
                os.unlink(last_message_path)
            except OSError:
                pass

        events = parse_codex_events(result.stdout or "")
        if last_message:
            output = last_message
        elif events.last_message is not None:
            output = events.last_message.strip()
        elif events.parsed:
            output = ""
        else:
            # Not a JSONL stream (older CLI / wrapper): plain text output.
            output = (result.stdout or "").strip()

        usage: dict = {}
        if events.saw_usage:
            usage = {
                # Codex reports cached tokens as a subset of input_tokens.
                "input_tokens": max(events.input_tokens - events.cached_input_tokens, 0),
                "cache_read_tokens": events.cached_input_tokens,
                "output_tokens": events.output_tokens,
            }

        # Codex can emit transient `error` events (e.g. stream retries) in a
        # run that still completes a turn; only a turn that never completed
        # counts as a failure when the exit code is 0.
        if result.returncode != 0 or (events.errors and not events.saw_usage):
            haystack = failure_haystack(
                None if events.parsed else result.stdout,
                result.stderr,
                *events.errors,
            )
            reset_at, rate_limited = classify_failure(haystack)
            detail = "\n".join(events.errors) or (result.stderr or "").strip()
            return RunResult(
                output=output,
                success=False,
                error=detail or f"codex exited with code {result.returncode}",
                rate_limit_reset_at=reset_at,
                rate_limited=rate_limited,
                session_id=events.thread_id,
                **usage,
            )

        return RunResult(
            output=output,
            success=True,
            session_id=events.thread_id,
            **usage,
        )
