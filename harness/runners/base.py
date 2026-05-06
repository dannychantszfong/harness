"""Abstract base for all code runners.

The harness's backbone is a coding agent — Claude Code CLI, Claude Code SDK,
or OpenAI Codex CLI. These three runners drive a full tool-using agent that
writes files, runs tests, and commits git inside its own session.

Direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) are NOT first-
class runners. They flow into the agentic runners as the underlying *model*:

  • ANTHROPIC_API_KEY            → Claude Code / SDK (default Anthropic auth)
  • ANTHROPIC_BASE_URL +
    ANTHROPIC_AUTH_TOKEN         → Claude Code via OpenRouter or proxy
  • OPENAI_API_KEY               → Codex
  • codex --oss --local-provider → Codex via lmstudio / ollama for OSS models

This means there's exactly one execution model: a tool-using agent in a
subprocess. Single-turn API calls without file I/O are not supported here.

In orchestration_mode="runner" all four agents (planner, initializer,
generator, evaluator) share the runner. In orchestration_mode="api" the
planner/evaluator/initializer fall back to the Anthropic API for structured
responses while the generator still uses the runner.
"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import os
from typing import Optional


class RunnerRateLimitedError(RuntimeError):
    """Raised when a subscription runner reports its usage limit was hit.

    Carries the parsed reset time when the runner reports one. Some providers
    only say that a cap was hit, so reset_at can be None.
    """

    def __init__(self, reset_at: datetime | None = None, raw_message: str = ""):
        self.reset_at = reset_at
        self.raw_message = raw_message
        if reset_at is not None:
            message = f"Subscription rate limit hit; resets at {reset_at.isoformat()}"
        else:
            message = "Runner usage limit hit; no reset time was reported"
        super().__init__(message)


class RunnerType(str, Enum):
    # All runners are agentic and use a coding-agent subscription (Pro/Max
    # for Claude Code, OpenAI for Codex). Direct API providers plug into
    # these via env vars — they are never their own runner.
    SUBPROCESS  = "subprocess"   # Claude Code CLI  — claude --print
    SDK         = "sdk"          # Claude Code SDK  — claude_code_sdk
    CODEX       = "codex"        # OpenAI Codex CLI — codex (CLI tool)

    @classmethod
    def agentic(cls) -> list["RunnerType"]:
        return [cls.SUBPROCESS, cls.SDK, cls.CODEX]

    @classmethod
    def api_based(cls) -> list["RunnerType"]:
        """Always empty — kept so existing callers don't break.

        API providers are no longer first-class runners; they plug into
        the agentic runners via env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY,
        ANTHROPIC_BASE_URL, codex --oss --local-provider, etc.).
        """
        return []

    @classmethod
    def choices(cls) -> list[str]:
        return [r.value for r in cls]

    @classmethod
    def menu(cls) -> str:
        lines = [
            "",
            "  ┌── Coding-agent runners (subscription billing, full file I/O) ─────────┐",
            "  │  subprocess  — Claude Code CLI     (Claude Pro / Max subscription)     │",
            "  │  sdk         — Claude Code SDK     (Claude Pro / Max subscription)     │",
            "  │  codex       — OpenAI Codex CLI    (OpenAI subscription)               │",
            "  └────────────────────────────────────────────────────────────────────────┘",
            "",
            "  API providers (Anthropic, OpenAI, Gemini, OpenRouter) plug into the",
            "  three runners above via env vars rather than being separate runners.",
        ]
        return "\n".join(lines)


@dataclass
class RunResult:
    """Unified result from any runner."""
    output: str
    success: bool
    error: Optional[str] = None
    # Token/cost data — populated by API runners; None for agentic runners
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    # Tool calls observed (SDK runner only)
    tool_calls_observed: list[str] = field(default_factory=list)
    # Subscription rate-limit reset time (UTC) when the runner hit a usage cap.
    # When set, agents convert this into RunnerRateLimitedError so the
    # orchestrator can react gracefully (auto-resume) instead of crashing.
    rate_limit_reset_at: Optional[datetime] = None
    rate_limited: bool = False


@dataclass
class PreflightResult:
    """Result of a runner pre-flight check."""
    ok: bool
    summary: str        # one-line human label, e.g. "Claude Code CLI  ·  subscription billing"
    details: str        # what is active, e.g. binary path or model name
    warning: Optional[str] = None   # non-fatal caveat to show the user
    error: Optional[str] = None     # fatal — runner cannot start


class CodeRunner(ABC):
    """Interface for running an agentic implementation session."""

    def __init__(self, config) -> None:
        self.config = config

    def subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in (getattr(self.config, "code_runner_env", {}) or {}).items():
            env[key] = _expand_env_value(value)
        return env

    @contextmanager
    def profile_env(self):
        extra = getattr(self.config, "code_runner_env", {}) or {}
        old_values: dict[str, str | None] = {}
        try:
            for key, value in extra.items():
                old_values[key] = os.environ.get(key)
                os.environ[key] = _expand_env_value(value)
            yield
        finally:
            for key, old in old_values.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    @property
    @abstractmethod
    def runner_type(self) -> RunnerType:
        ...

    @abstractmethod
    def preflight(self) -> PreflightResult:
        """Check whether this runner is ready to use.

        Called before the first feature so problems are surfaced immediately,
        not mid-run. Must not make real API calls.
        """
        ...

    @abstractmethod
    def implement(self, prompt: str, cwd: str, timeout_seconds: int = 600) -> RunResult:
        """Run one complete implementation session for a single feature."""
        ...


def _expand_env_value(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], value)
    if value.startswith("$") and len(value) > 1:
        return os.environ.get(value[1:], value)
    return value
