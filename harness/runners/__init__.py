from harness.runners.base import CodeRunner, RunResult, RunnerType, PreflightResult
from harness.runners.subprocess_runner import SubprocessRunner
from harness.runners.sdk_runner import SDKRunner
from harness.runners.codex_runner import CodexRunner


def create_runner(runner_type: RunnerType, config) -> CodeRunner:
    """Factory — return the correct CodeRunner for the given type.

    Only the three agentic runners (Claude Code CLI, Claude Code SDK, Codex CLI)
    exist as first-class runners. API providers like Anthropic, OpenAI, Gemini,
    or OpenRouter plug into one of these via env vars (e.g. ANTHROPIC_API_KEY,
    OPENAI_API_KEY, ANTHROPIC_BASE_URL for OpenRouter routing through Claude
    Code, or `codex --oss --local-provider` for OSS providers).
    """
    mapping = {
        RunnerType.SUBPROCESS:  SubprocessRunner,
        RunnerType.SDK:         SDKRunner,
        RunnerType.CODEX:       CodexRunner,
    }
    cls = mapping.get(runner_type)
    if cls is None:
        raise ValueError(f"Unknown runner type: {runner_type!r}")
    return cls(config)


__all__ = [
    "CodeRunner", "RunResult", "RunnerType", "PreflightResult",
    "create_runner",
    "SubprocessRunner", "SDKRunner", "CodexRunner",
]
