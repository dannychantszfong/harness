"""SDK Runner — uses claude_code_sdk for programmatic Claude Code control.

Billing:  your Claude subscription (Pro / Max)
File I/O: full — Claude Code writes files, runs commands, commits git
Best for: users who want subscription billing AND structured output/cost data

Requires: pip install claude-code-sdk
Docs:     https://github.com/anthropics/claude-code

The SDK streams structured message objects so the harness can observe every
tool call (Write, Bash, etc.) in real time and extract cost data.
"""

import asyncio
from rich.console import Console

from harness.runners.base import CodeRunner, PreflightResult, RunResult, RunnerType

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


def _check_sdk() -> tuple[bool, str]:
    try:
        import claude_code_sdk  # noqa: F401
        return True, ""
    except ImportError:
        return False, "claude_code_sdk not installed. Run: pip install claude-code-sdk"


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
        return PreflightResult(
            ok=True,
            summary="Claude Code SDK  ·  subscription billing  ·  full file I/O",
            details=(
                f"claude_code_sdk {version}  "
                f"(same subscription as CLI, adds structured tool-call visibility)   "
                f"Model: {model}"
            ),
        )

    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        available, err = _check_sdk()
        if not available:
            return RunResult(output="", success=False, error=err)

        console.print("[dim]Runner: Claude Code SDK — using subscription[/dim]")

        try:
            with self.profile_env():
                return asyncio.run(self._run_async(prompt, cwd, timeout_seconds))
        except Exception as exc:
            text = str(exc)
            return RunResult(
                output="",
                success=False,
                error=text,
                rate_limited=_looks_rate_limited(text),
            )

    async def _run_async(self, prompt: str, cwd: str, timeout_seconds: int) -> RunResult:
        from claude_code_sdk import query, ClaudeCodeOptions

        output_parts: list[str] = []
        tool_calls: list[str] = []
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0

        async def _stream():
            nonlocal input_tokens, output_tokens, cost_usd

            async for message in query(
                prompt=prompt,
                options=ClaudeCodeOptions(
                    cwd=cwd,
                    max_turns=50,
                    model=getattr(self.config, "code_runner_model", None),
                    # YOLO — matches `claude --print --dangerously-skip-permissions`
                    # used by SubprocessRunner. Required for unattended runs.
                    permission_mode="bypassPermissions",
                ),
            ):
                msg_type = type(message).__name__

                # Collect text from assistant messages
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            output_parts.append(block.text)
                            print(block.text, end="", flush=True)
                        # Track which tools Claude Code is invoking
                        if hasattr(block, "name"):
                            tool_calls.append(block.name)
                            console.print(f"\n  [dim]→ tool: {block.name}[/dim]")

                # Extract cost/token data from result message
                if msg_type == "ResultMessage":
                    if hasattr(message, "usage") and message.usage:
                        input_tokens = getattr(message.usage, "input_tokens", 0) or 0
                        output_tokens = getattr(message.usage, "output_tokens", 0) or 0
                    if hasattr(message, "cost_usd") and message.cost_usd:
                        cost_usd = message.cost_usd

        await asyncio.wait_for(_stream(), timeout=timeout_seconds)
        print()

        return RunResult(
            output="\n".join(output_parts).strip(),
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            tool_calls_observed=tool_calls,
        )
