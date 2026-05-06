"""Gemini API Runner — calls the Google Gemini API.

Billing:  pay-per-token (GEMINI_API_KEY)
File I/O: none — model outputs text only
Model:    configured via config.generator_model (default: gemini-2.5-pro)

Install:  pip install google-generativeai
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

_DEFAULT_MODEL = "gemini-2.5-pro"


class GeminiAPIRunner(CodeRunner):
    """Calls the Google Gemini API via google-generativeai."""

    runner_type = RunnerType.GEMINI

    def preflight(self) -> PreflightResult:
        key = os.environ.get("GEMINI_API_KEY") or getattr(self.config, "gemini_api_key", None) or ""
        model = getattr(self.config, "generator_model", _DEFAULT_MODEL)
        if not key:
            return PreflightResult(
                ok=False,
                summary="Google Gemini API  ·  pay-per-token  ·  text output only",
                details="",
                error="GEMINI_API_KEY is not set.",
            )
        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "****"
        return PreflightResult(
            ok=True,
            summary="Google Gemini API  ·  pay-per-token  ·  text output only",
            details=f"Key: {masked}   Model: {model}   No file I/O — outputs text descriptions only.",
            warning="API runner produces text only. Files are NOT written to disk automatically.",
        )

    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        try:
            import google.generativeai as genai
        except ImportError:
            return RunResult(
                output="",
                success=False,
                error="google-generativeai not installed. Run: pip install google-generativeai",
            )

        api_key = os.environ.get("GEMINI_API_KEY") or getattr(self.config, "gemini_api_key", None)
        if not api_key:
            return RunResult(
                output="",
                success=False,
                error="GEMINI_API_KEY not set. Export it or add gemini_api_key to your config.",
            )

        model_name = getattr(self.config, "generator_model", _DEFAULT_MODEL)
        console.print(f"[dim]Runner: Google Gemini API ({model_name}) — pay-per-token[/dim]")

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=_SYSTEM,
        )

        full_text = ""
        input_tokens = 0
        output_tokens = 0

        response = model.generate_content(
            prompt,
            stream=True,
            generation_config=genai.GenerationConfig(max_output_tokens=16_000),
        )

        for chunk in response:
            text = chunk.text or ""
            print(text, end="", flush=True)
            full_text += text

        print()

        try:
            usage = response.usage_metadata
            input_tokens = usage.prompt_token_count or 0
            output_tokens = usage.candidates_token_count or 0
        except Exception:
            pass

        # Gemini 2.5 Pro pricing (~$1.25/$10 per 1M tokens)
        cost = (input_tokens * 1.25 + output_tokens * 10) / 1_000_000

        return RunResult(
            output=full_text,
            success=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
