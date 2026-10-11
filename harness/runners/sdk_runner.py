"""SDK Runner — uses claude_code_sdk for programmatic Claude Code control.

Billing:  your Claude subscription (Pro / Max)
File I/O: full — Claude Code writes files, runs commands, commits git
Best for: users who want subscription billing AND structured output/cost data

Requires: pip install claude-code-sdk==0.0.25
Docs:     https://github.com/anthropics/claude-code

The SDK streams structured message objects so the harness can observe every
tool call (Write, Bash, etc.) in real time and extract cost data.

ADR-014: the package is frozen at 0.0.25 (it still spawns the same `claude`
CLI). Migration to claude-agent-sdk is planned for 1.1 because it changes
more than the import name (ClaudeAgentOptions, no Claude Code system-prompt
preset and no settings sources by default).
"""

import asyncio
from typing import Any, Optional

from rich.console import Console

from harness.runners._rate_limit import classify_failure, failure_haystack
from harness.runners.base import (
    WRITE_TOOLS,
    CodeRunner,
    PreflightResult,
    RunResult,
    RunnerAccess,
    RunnerType,
    missing_cwd_result,
)

console = Console()

PINNED_SDK_VERSION = "0.0.25"
DEFAULT_SDK_MAX_TURNS = 100


def _check_sdk() -> tuple[bool, str]:
    try:
        import claude_code_sdk  # noqa: F401
        return True, ""
    except ImportError:
        return False, (
            f"claude_code_sdk not installed. Run: pip install claude-code-sdk=={PINNED_SDK_VERSION}"
        )


def _usage_field(usage: Any, key: str) -> int:
    if isinstance(usage, dict):
        return int(usage.get(key) or 0)
    return int(getattr(usage, key, 0) or 0)


class SDKRunner(CodeRunner):
    """Runs Claude Code via the claude_code_sdk Python package."""

    runner_type = RunnerType.SDK

    def preflight(self) -> PreflightResult:
        available, err = _check_sdk()
        if not available:
            return PreflightResult(
                ok=False,
                summary="Claude Code SDK  ·  subscription billing  ·  full file I/O",
                details="",
                error=f"{err}\nNote: SDK and CLI both use your Claude subscription — SDK gives structured output.",
            )
        try:
            import claude_code_sdk
            version = getattr(claude_code_sdk, "__version__", "installed")
        except Exception:
            version = "installed"
        model = getattr(self.config, "code_runner_model", None) or "runner default"
        warning = None
        if version not in (PINNED_SDK_VERSION, "installed"):
            warning = (
                f"claude_code_sdk {version} is installed; Harness 1.0 is tested with "
                f"{PINNED_SDK_VERSION} (pip install claude-code-sdk=={PINNED_SDK_VERSION})."
            )
        return PreflightResult(
            ok=True,
            summary="Claude Code SDK  ·  subscription billing  ·  full file I/O",
            details=(
                f"claude_code_sdk {version}  "
                f"(same subscription as CLI, adds structured tool-call visibility)   "
                f"Model: {model}\n"
                "The sdk runner uses the frozen claude-code-sdk; migration to "
                "claude-agent-sdk is planned for 1.1."
            ),
            warning=warning,
        )

    def _options_kwargs(self, cwd: str, access: Optional[RunnerAccess] = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "max_turns": int(getattr(self.config, "sdk_max_turns", None) or DEFAULT_SDK_MAX_TURNS),
            "model": getattr(self.config, "code_runner_model", None),
            # YOLO — matches `claude --print --dangerously-skip-permissions`
            # used by SubprocessRunner. Required for unattended runs.
            "permission_mode": "bypassPermissions",
        }
        if access is not None and access.restricted:
            # Non-interactive: anything outside the allow-list is denied.
            kwargs["permission_mode"] = "default"
            kwargs["allowed_tools"] = list(access.allowed_tools)
            if access.read_only:
                kwargs["disallowed_tools"] = list(WRITE_TOOLS)
        if access is not None and access.mcp_config:
            # A dict is the form every claude-code-sdk release accepts.
            from harness.agents.browser import load_mcp_servers

            kwargs["mcp_servers"] = load_mcp_servers(access.mcp_config)
        return kwargs

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
        available, err = _check_sdk()
        if not available:
            return RunResult(output="", success=False, error=err)

        console.print("[dim]Runner: Claude Code SDK — using subscription[/dim]")

        try:
            with self.profile_env():
                return asyncio.run(self._run_async(prompt, cwd, timeout_seconds, access))
        except asyncio.TimeoutError:
            return RunResult(
                output="",
                success=False,
                error=f"Claude Code SDK timed out after {timeout_seconds}s",
                timed_out=True,
            )
        except Exception as exc:
            text = str(exc) or type(exc).__name__
            reset_at, rate_limited = classify_failure(text)
            return RunResult(
                output="",
                success=False,
                error=text,
                rate_limit_reset_at=reset_at,
                rate_limited=rate_limited,
            )

    async def _run_async(
        self,
        prompt: str,
        cwd: str,
        timeout_seconds: int,
        access: Optional[RunnerAccess] = None,
    ) -> RunResult:
        from claude_code_sdk import query, ClaudeCodeOptions

        output_parts: list[str] = []
        tool_calls: list[str] = []
        final: dict[str, Any] = {}

        stream = query(prompt=prompt, options=ClaudeCodeOptions(**self._options_kwargs(cwd, access)))

        async def _consume() -> None:
            async for message in stream:
                # Collect text from assistant messages
                for block in getattr(message, "content", None) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text:
                        output_parts.append(text)
                        print(text, end="", flush=True)
                    # Track which tools Claude Code is invoking
                    name = getattr(block, "name", None)
                    if isinstance(name, str) and name:
                        tool_calls.append(name)
                        console.print(f"\n  [dim]→ tool: {name}[/dim]")

                if type(message).__name__ == "ResultMessage":
                    final["result"] = getattr(message, "result", None)
                    final["usage"] = getattr(message, "usage", None)
                    final["cost"] = getattr(message, "total_cost_usd", None)
                    final["is_error"] = bool(getattr(message, "is_error", False))
                    final["subtype"] = getattr(message, "subtype", None)
                    final["session_id"] = getattr(message, "session_id", None)

        try:
            await asyncio.wait_for(_consume(), timeout=timeout_seconds)
        finally:
            # Closing the generator runs the SDK's transport cleanup, which
            # terminates the `claude` subprocess (matters on timeout).
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass
        print()

        usage = final.get("usage")
        tokens: dict[str, Any] = {}
        if usage:
            tokens = {
                "input_tokens": _usage_field(usage, "input_tokens")
                + _usage_field(usage, "cache_creation_input_tokens"),
                "output_tokens": _usage_field(usage, "output_tokens"),
                "cache_read_tokens": _usage_field(usage, "cache_read_input_tokens"),
            }

        result_text = final.get("result")
        output = (
            result_text.strip()
            if isinstance(result_text, str)
            else "\n".join(output_parts).strip()  # fallback: no final result
        )

        if final.get("is_error"):
            subtype = final.get("subtype") or "error"
            error = f"Claude Code SDK session ended with {subtype}"
            if output:
                error += f": {output[-1500:]}"
            # Classify on the session's own final message only — never on
            # the transcript fallback, where a generator building a rate
            # limiter talks about "429" (P-14). No result → not a cap.
            reset_at, rate_limited = classify_failure(
                failure_haystack(result_text if isinstance(result_text, str) else "", None)
            )
            return RunResult(
                output=output,
                success=False,
                error=error,
                cost_usd=final.get("cost"),
                tool_calls_observed=tool_calls,
                session_id=final.get("session_id"),
                rate_limit_reset_at=reset_at,
                rate_limited=rate_limited,
                **tokens,
            )

        return RunResult(
            output=output,
            success=True,
            cost_usd=final.get("cost"),
            tool_calls_observed=tool_calls,
            session_id=final.get("session_id"),
            **tokens,
        )
