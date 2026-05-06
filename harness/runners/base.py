"""Abstract base for all code runners.

Two families of runner:

  AGENTIC  — drives a full tool-using agent that writes files to disk.
             Uses your subscription (no extra API bill).
             Options: Claude Code CLI, Claude Code SDK, OpenAI Codex CLI.

  API      — single-turn Anthropic/OpenAI-compatible call.
             Pay-per-token; model outputs text only (no file I/O).
             Options: Anthropic, OpenAI, Gemini, OpenRouter.

GeneratorAgent always uses the runner. Planner/evaluator/initializer also
use the runner in orchestration_mode="runner"; in orchestration_mode="api"
they call the Anthropic API directly for structured responses.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class RunnerRateLimitedError(RuntimeError):
    """Raised when a subscription runner reports its usage limit was hit.

    Carries the parsed reset time (timezone-aware UTC) so the orchestrator
    can either inform the user or schedule an auto-resume.
    """

    def __init__(self, reset_at: datetime, raw_message: str = ""):
        self.reset_at = reset_at
        self.raw_message = raw_message
        super().__init__(
            f"Subscription rate limit hit; resets at {reset_at.isoformat()}"
        )


class RunnerType(str, Enum):
    # ── Agentic (subscription-based, full file I/O) ──────────────────────────
    SUBPROCESS  = "subprocess"   # Claude Code CLI  — claude --print
    SDK         = "sdk"          # Claude Code SDK  — claude_code_sdk
    CODEX       = "codex"        # OpenAI Codex CLI — codex (CLI tool)

    # ── API (pay-per-token, text output only) ────────────────────────────────
    ANTHROPIC   = "anthropic"    # Anthropic API    — ANTHROPIC_API_KEY
    OPENAI      = "openai"       # OpenAI API       — OPENAI_API_KEY
    GEMINI      = "gemini"       # Google Gemini    — GEMINI_API_KEY
    OPENROUTER  = "openrouter"   # OpenRouter       — OPENROUTER_API_KEY

    @classmethod
    def agentic(cls) -> list["RunnerType"]:
        return [cls.SUBPROCESS, cls.SDK, cls.CODEX]

    @classmethod
    def api_based(cls) -> list["RunnerType"]:
        return [cls.ANTHROPIC, cls.OPENAI, cls.GEMINI, cls.OPENROUTER]

    @classmethod
    def choices(cls) -> list[str]:
        return [r.value for r in cls]

    @classmethod
    def menu(cls) -> str:
        lines = [
            "",
            "  ┌── AGENTIC runners (use your subscription, full file I/O) ──────────────┐",
            "  │  subprocess  — Claude Code CLI     (claude subscription / Pro / Max)    │",
            "  │  sdk         — Claude Code SDK     (claude subscription / Pro / Max)    │",
            "  │  codex       — OpenAI Codex CLI    (openai subscription required)       │",
            "  ├── API runners (pay-per-token, model outputs text only) ─────────────────┤",
            "  │  anthropic   — Anthropic API        env: ANTHROPIC_API_KEY              │",
            "  │  openai      — OpenAI API           env: OPENAI_API_KEY                 │",
            "  │  gemini      — Google Gemini API    env: GEMINI_API_KEY                 │",
            "  │  openrouter  — OpenRouter           env: OPENROUTER_API_KEY             │",
            "  └────────────────────────────────────────────────────────────────────────┘",
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
