"""API Runner — direct Anthropic SDK call.

Billing:  pay-per-token (API credits, separate from subscription)
File I/O: none — the model outputs text only
Best for: testing harness logic without needing Claude Code installed

Limitation: the generator produces a text description of what it would do,
not actual file changes. You would need to apply changes manually or add
file-writing tools to the API call. Use subprocess or SDK for real builds.
"""

import os
import anthropic
from rich.console import Console

from harness.runners.base import CodeRunner, PreflightResult, RunResult, RunnerType

console = Console()

_SYSTEM = """You are a senior full-stack engineer.
Implement the requested feature. Since you cannot write files directly in
this mode, output your complete implementation as a structured response:

1. List every file you would create or modify.
2. For each file, output the full file content inside a fenced code block
   labelled with the file path.
3. Write a self-evaluation: what you built, edge cases handled, concerns.

Be exhaustive — include complete file contents, not diffs or snippets.
"""


class APIRunner(CodeRunner):
    """Calls the Anthropic API directly. No file writing — text output only."""

    runner_type = RunnerType.ANTHROPIC

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client = anthropic.Anthropic()

    def preflight(self) -> PreflightResult:
        key = os.environ.get("ANTHROPIC_API_KEY") or ""
        model = getattr(self.config, "generator_model", "claude-opus-4-7")
        if not key:
            return PreflightResult(
                ok=False,
                summary="Anthropic API  ·  pay-per-token  ·  text output only",
                details="",
                error="ANTHROPIC_API_KEY is not set.",
            )
        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "****"
        return PreflightResult(
            ok=True,
            summary="Anthropic API  ·  pay-per-token  ·  text output only",
            details=f"Key: {masked}   Model: {model}   No file I/O — outputs text descriptions only.",
            warning="API runner produces text only. Files are NOT written to disk automatically.",
        )

    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        console.print("[dim]Runner: Anthropic API (text only — no file I/O)[/dim]")

        full_text = ""
        input_tokens = 0
        output_tokens = 0

        with self._client.messages.stream(
            model=self.config.generator_model,
            max_tokens=16_000,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for chunk in stream.text_stream:
                print(chunk, end="", flush=True)
                full_text += chunk
            print()
            final = stream.get_final_message()
            input_tokens = final.usage.input_tokens
            output_tokens = final.usage.output_tokens

        # Rough cost estimate at Opus 4.7 pricing
        cost = (input_tokens * 15 + output_tokens * 75) / 1_000_000

        return RunResult(
            output=full_text,
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
