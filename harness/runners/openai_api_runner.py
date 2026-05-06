"""OpenAI API Runner — calls the OpenAI chat completions API.

Billing:  pay-per-token (OPENAI_API_KEY)
File I/O: none — model outputs text only
Model:    configured via config.generator_model (default: gpt-4o)

Install:  pip install openai
"""

import os
from rich.console import Console

from harness.runners.base import CodeRunner, PreflightResult, RunResult, RunnerType

console = Console()

_SYSTEM = """You are a senior full-stack engineer implementing a software feature.
Since you cannot write files directly, output your complete implementation as:

1. A list of every file to create or modify.
2. Full file contents in fenced code blocks labelled with the file path.
3. A self-evaluation: what you built, edge cases, concerns.

Be exhaustive — complete file contents only, no partial diffs.
"""

_DEFAULT_MODEL = "gpt-4o"


class OpenAIAPIRunner(CodeRunner):
    """Calls the OpenAI chat completions API."""

    runner_type = RunnerType.OPENAI

    def preflight(self) -> PreflightResult:
        key = os.environ.get("OPENAI_API_KEY") or getattr(self.config, "openai_api_key", None) or ""
        model = getattr(self.config, "generator_model", _DEFAULT_MODEL)
        if not key:
            return PreflightResult(
                ok=False,
                summary="OpenAI API  ·  pay-per-token  ·  text output only",
                details="",
                error="OPENAI_API_KEY is not set.",
            )
        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "****"
        return PreflightResult(
            ok=True,
            summary="OpenAI API  ·  pay-per-token  ·  text output only",
            details=f"Key: {masked}   Model: {model}   No file I/O — outputs text descriptions only.",
            warning="API runner produces text only. Files are NOT written to disk automatically.",
        )

    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        try:
            from openai import OpenAI
        except ImportError:
            return RunResult(
                output="",
                success=False,
                error="openai package not installed. Run: pip install openai",
            )

        api_key = os.environ.get("OPENAI_API_KEY") or getattr(self.config, "openai_api_key", None)
        if not api_key:
            return RunResult(
                output="",
                success=False,
                error="OPENAI_API_KEY not set. Export it or add openai_api_key to your config.",
            )

        model = getattr(self.config, "generator_model", _DEFAULT_MODEL)
        console.print(f"[dim]Runner: OpenAI API ({model}) — pay-per-token[/dim]")

        client = OpenAI(api_key=api_key)

        full_text = ""
        input_tokens = 0
        output_tokens = 0

        stream = client.chat.completions.create(
            model=model,
            max_tokens=16_000,
            stream=True,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )

        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            print(delta, end="", flush=True)
            full_text += delta

        print()

        # Get usage from final chunk if available
        try:
            usage = stream.get_final_completion().usage
            if usage:
                input_tokens = usage.prompt_tokens or 0
                output_tokens = usage.completion_tokens or 0
        except Exception:
            pass

        # Rough cost estimate at GPT-4o pricing ($5/$15 per 1M tokens)
        cost = (input_tokens * 5 + output_tokens * 15) / 1_000_000

        return RunResult(
            output=full_text,
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
