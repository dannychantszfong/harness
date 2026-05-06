"""OpenRouter API Runner — routes to any model via OpenRouter.

Billing:  pay-per-token (OPENROUTER_API_KEY) — model-dependent pricing
File I/O: none — model outputs text only
Model:    any OpenRouter-supported model ID, e.g.:
            anthropic/claude-opus-4-7
            openai/gpt-4o
            google/gemini-2.5-pro
            meta-llama/llama-3-70b-instruct

OpenRouter uses the OpenAI-compatible API, so the `openai` package works.

Install:  pip install openai
Docs:     https://openrouter.ai/docs
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

_DEFAULT_MODEL = "anthropic/claude-opus-4-7"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAPIRunner(CodeRunner):
    """Calls OpenRouter using the OpenAI-compatible API."""

    runner_type = RunnerType.OPENROUTER

    def preflight(self) -> PreflightResult:
        key = (
            os.environ.get("OPENROUTER_API_KEY")
            or getattr(self.config, "openrouter_api_key", None)
            or ""
        )
        model = getattr(self.config, "generator_model", _DEFAULT_MODEL)
        if not key:
            return PreflightResult(
                ok=False,
                summary="OpenRouter  ·  pay-per-token  ·  text output only",
                details="",
                error="OPENROUTER_API_KEY is not set.",
            )
        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "****"
        return PreflightResult(
            ok=True,
            summary="OpenRouter  ·  pay-per-token  ·  text output only",
            details=f"Key: {masked}   Model: {model}   Pricing varies by model — check openrouter.ai/activity.",
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

        api_key = (
            os.environ.get("OPENROUTER_API_KEY")
            or getattr(self.config, "openrouter_api_key", None)
        )
        if not api_key:
            return RunResult(
                output="",
                success=False,
                error="OPENROUTER_API_KEY not set. Get one at https://openrouter.ai/keys",
            )

        model = getattr(self.config, "generator_model", _DEFAULT_MODEL)
        console.print(f"[dim]Runner: OpenRouter ({model}) — pay-per-token[/dim]")

        client = OpenAI(
            api_key=api_key,
            base_url=_OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/anthropics/claude-agent-harness",
                "X-Title": "Claude Agent Harness",
            },
        )

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

        try:
            usage = stream.get_final_completion().usage
            if usage:
                input_tokens = usage.prompt_tokens or 0
                output_tokens = usage.completion_tokens or 0
        except Exception:
            pass

        return RunResult(
            output=full_text,
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=None,  # OpenRouter pricing varies per model; check dashboard
        )
