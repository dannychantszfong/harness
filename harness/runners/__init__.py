from harness.runners.base import CodeRunner, RunResult, RunnerType, PreflightResult
from harness.runners.api_runner import APIRunner
from harness.runners.subprocess_runner import SubprocessRunner
from harness.runners.sdk_runner import SDKRunner
from harness.runners.codex_runner import CodexRunner
from harness.runners.openai_api_runner import OpenAIAPIRunner
from harness.runners.gemini_api_runner import GeminiAPIRunner
from harness.runners.openrouter_api_runner import OpenRouterAPIRunner


def create_runner(runner_type: RunnerType, config) -> CodeRunner:
    """Factory — return the correct CodeRunner for the given type."""
    mapping = {
        RunnerType.ANTHROPIC:   APIRunner,
        RunnerType.SUBPROCESS:  SubprocessRunner,
        RunnerType.SDK:         SDKRunner,
        RunnerType.CODEX:       CodexRunner,
        RunnerType.OPENAI:      OpenAIAPIRunner,
        RunnerType.GEMINI:      GeminiAPIRunner,
        RunnerType.OPENROUTER:  OpenRouterAPIRunner,
    }
    cls = mapping.get(runner_type)
    if cls is None:
        raise ValueError(f"Unknown runner type: {runner_type!r}")
    return cls(config)


__all__ = [
    "CodeRunner", "RunResult", "RunnerType", "create_runner",
    "APIRunner", "SubprocessRunner", "SDKRunner", "CodexRunner",
    "OpenAIAPIRunner", "GeminiAPIRunner", "OpenRouterAPIRunner",
]
