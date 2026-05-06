"""Codex Runner — invokes the OpenAI Codex CLI (`codex` binary).

Billing:  your OpenAI subscription / credits attached to the Codex CLI
File I/O: full — Codex writes files, runs commands, same model as CLI
Best for: users already on the OpenAI ecosystem

Install:  npm install -g @openai/codex   (or via the OpenAI app)
Docs:     https://github.com/openai/codex
"""

import subprocess
import shutil
from rich.console import Console

from harness.runners.base import CodeRunner, PreflightResult, RunResult, RunnerType
from harness.ui import QuietAnimator

console = Console()

_RATE_LIMIT_HINTS = (
    "rate limit",
    "usage limit",
    "quota",
    "too many requests",
    "429",
)


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _RATE_LIMIT_HINTS)


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
                ["codex", "--version"], capture_output=True, text=True, timeout=5
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

    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        if not shutil.which("codex"):
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

        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
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
        cmd.append(prompt)

        try:
            with QuietAnimator.from_config(self.config, phase="coding"):
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    env=self.subprocess_env(),
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            return RunResult(
                output="",
                success=False,
                error=f"Codex timed out after {timeout_seconds}s",
            )
        except FileNotFoundError:
            return RunResult(
                output="",
                success=False,
                error="`codex` binary not found. Is the Codex CLI installed?",
            )

        if result.returncode != 0:
            combined = f"{result.stdout or ''}\n{result.stderr or ''}"
            return RunResult(
                output=result.stdout,
                success=False,
                error=result.stderr or f"codex exited with code {result.returncode}",
                rate_limited=_looks_rate_limited(combined),
            )

        return RunResult(
            output=result.stdout.strip(),
            success=True,
        )
