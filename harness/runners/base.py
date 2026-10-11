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
from pathlib import Path
import signal
import subprocess
import sys
from typing import Optional

# Fallback when a config object does not carry runner_timeout_seconds.
DEFAULT_RUNNER_TIMEOUT_SECONDS = 1800


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


class RunnerTimeoutError(RuntimeError):
    """Raised by the generator when its runner session hit the timeout.

    The orchestrator records a failed iteration with a clear reason instead
    of handing the timeout text to the evaluator as a "self-evaluation".
    """

    def __init__(self, timeout_seconds: int | None = None, raw_message: str = ""):
        self.timeout_seconds = timeout_seconds
        self.raw_message = raw_message
        if timeout_seconds:
            message = f"Runner session timed out after {timeout_seconds}s"
        else:
            message = "Runner session timed out"
        super().__init__(message)


class RunnerTurnLimitError(RunnerTimeoutError):
    """The generator session ended because it used up its turn budget
    (sdk_max_turns, or --max-turns in the extra args). Recorded like a
    timeout: a failed, ungraded iteration with advice to work in steps."""

    def __init__(self, raw_message: str = ""):
        super().__init__(None, raw_message)
        self.args = ("Runner session hit its turn limit",)


class RunnerFailedError(RuntimeError):
    """The generator's runner failed for a reason other than a timeout, a
    turn limit or a usage cap: missing binary, expired sign-in, a crash.

    Not a self-evaluation to grade. The orchestrator retries once without
    counting the iteration, then stops the run with the feature left in
    progress, so a broken runner cannot march every feature to exhaustion.
    """

    def __init__(self, raw_message: str = ""):
        self.raw_message = raw_message
        super().__init__(raw_message or "Runner session failed")


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
    # Token/cost data reported by the runtime (None when it reports nothing).
    # input_tokens counts *new* prompt tokens (uncached input + cache writes);
    # cache_read_tokens counts prompt tokens served from the prompt cache.
    # cost_usd is the runtime's own figure — on a subscription it is the
    # API-equivalent list price, not what the user is billed.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    # True when the session was killed by the runner timeout.
    timed_out: bool = False
    # True when the session ended because it used up its turn budget.
    turn_limited: bool = False
    # Tool calls observed (SDK runner only)
    tool_calls_observed: list[str] = field(default_factory=list)
    # Subscription rate-limit reset time (UTC) when the runner hit a usage cap.
    # When set, agents convert this into RunnerRateLimitedError so the
    # orchestrator can react gracefully (auto-resume) instead of crashing.
    rate_limit_reset_at: Optional[datetime] = None
    rate_limited: bool = False


@dataclass(frozen=True)
class RunnerAccess:
    """What a runner session may do (C-06). None/default = full access.

    read_only: no file writes — Claude Code runs with --permission-mode
      dontAsk, an allow-list and Write/Edit disallowed; Codex runs in
      --sandbox read-only.
    allowed_tools: Claude Code permission rules for an allow-list session
      (e.g. "Read", "Bash(git diff *)"). Non-empty → restricted session.
    mcp_config: path to an MCP config JSON for this session (C-07).
    """
    read_only: bool = False
    allowed_tools: tuple[str, ...] = ()
    mcp_config: Optional[str] = None

    @property
    def restricted(self) -> bool:
        return self.read_only or bool(self.allowed_tools)


# Tools a read-only Claude Code session never gets, whatever the allow-list.
WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "MultiEdit", "NotebookEdit")


# Exact git invocations for read-only sessions. A wildcard rule such as
# "Bash(git log *)" would also allow `git log --output=<file>` (a write) or
# `git diff --no-index <outside file>` (a read outside the project).
SAFE_GIT_COMMANDS: tuple[str, ...] = (
    "git status",
    "git status --short",
    "git diff",
    "git diff --stat",
    "git diff HEAD",
    "git diff HEAD --stat",
    "git diff HEAD~1",
    "git diff HEAD~1 --stat",
    "git log --oneline -20",
    "git log --oneline -50",
    "git log -5 --stat",
    "git show --stat",
    "git show HEAD",
)


def default_read_only_tools(config) -> list[str]:
    """Read/search tools plus allow-listed commands: startup, tests, git.

    Which of these the agent uses, and in what order, is its own judgement;
    this is only the permission boundary.
    """
    tools = ["Read", "Grep", "Glob"]
    tools += [f"Bash({command})" for command in SAFE_GIT_COMMANDS]
    startup = getattr(config, "startup_command_for_platform", None)
    test = getattr(config, "test_command", None)
    for command in (c for c in (startup, test) if c):
        tools += [f"Bash({command})", f"Bash({command} *)"]
    return tools


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

    def _resolved_env(self) -> dict[str, str]:
        """Merged base + profile env, with $VAR / ${VAR} expansion applied.

        See HarnessConfig.active_runner_env for the layering rules.
        """
        return getattr(self.config, "active_runner_env", {}) or {}

    def subprocess_env(self) -> dict[str, str]:
        """Build a child-process env: parent env + the resolved overrides."""
        env = os.environ.copy()
        for key, value in self._resolved_env().items():
            env[key] = _expand_env_value(value)
        return env

    @contextmanager
    def profile_env(self):
        """Apply the resolved env to os.environ for the duration of a call.

        IMPORTANT: This mutates the global process env. It is used by the
        SDK runner because the SDK has no `env=` parameter to pass through.
        It is therefore unsafe under parallel execution within a single
        process — that's accepted today because feature execution is
        sequential. Any future move to parallel features should either
        (a) replace this with an SDK-specific env hook, or (b) gate the
        SDK runner to single-process mode.
        """
        extra = self._resolved_env()
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
    def implement(
        self,
        prompt: str,
        cwd: str,
        timeout_seconds: int | None = None,
        access: Optional[RunnerAccess] = None,
    ) -> RunResult:
        """Run one complete implementation session for a single feature.

        timeout_seconds=None means "use config.runner_timeout_seconds".
        access=None means full access (generator / initializer).
        """
        ...

    @staticmethod
    def _claude_permission_args(access: Optional[RunnerAccess]) -> list[str]:
        """Claude Code CLI permission flags for a session (subprocess runner)."""
        if access is None or not access.restricted:
            return ["--dangerously-skip-permissions"]  # required for unattended use
        args = ["--permission-mode", "dontAsk"]
        if access.allowed_tools:
            args += ["--allowedTools", *access.allowed_tools]
        if access.read_only:
            args += ["--disallowedTools", *WRITE_TOOLS]
        return args

    def _timeout(self, timeout_seconds: int | None) -> int:
        if timeout_seconds:
            return timeout_seconds
        return int(
            getattr(self.config, "runner_timeout_seconds", None)
            or DEFAULT_RUNNER_TIMEOUT_SECONDS
        )


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a child and everything it started.

    Agent CLIs are often shims (`codex.cmd` → node on Windows, a shell
    wrapper elsewhere): killing only the direct child leaves the real agent
    running and its pipes open.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # child leads its own session
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _linux_pdeathsig():
    """preexec_fn giving a child SIGKILL when Harness dies, or None.

    Resolved once in the parent: the child only makes one C call between
    fork and exec.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    pr_set_pdeathsig = 1
    parent = os.getpid()

    def _preexec() -> None:
        prctl(pr_set_pdeathsig, signal.SIGKILL)
        if os.getppid() != parent:  # Harness died before prctl took effect
            os.kill(os.getpid(), signal.SIGKILL)

    return _preexec


def child_popen_kwargs() -> dict:
    """Popen kwargs for an agent session or an allow-listed command.

    POSIX: the child leads its own session, so kill_process_tree can kill
    the whole group; on Linux it also dies with Harness (PDEATHSIG). Windows
    needs nothing here: the kill-on-close job (process_guard) covers it.
    """
    if os.name == "nt":
        return {}
    kwargs: dict = {"start_new_session": True}
    preexec = _linux_pdeathsig()
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    return kwargs


def run_process(
    cmd: list[str],
    *,
    cwd: str,
    env: Optional[dict[str, str]] = None,
    input: Optional[str] = None,  # noqa: A002 — mirrors subprocess.run
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True) that enforces its timeout.

    On timeout, Ctrl+C, SIGTERM/SIGHUP (SystemExit via process_guard) the
    whole process tree is killed before the exception propagates, so a hung
    agent behind a shim cannot hang Harness and no agent outlives it.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **child_popen_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise subprocess.TimeoutExpired(cmd, timeout)
    except BaseException:
        # KeyboardInterrupt, or SystemExit from a SIGTERM/SIGHUP handler: a
        # child in its own session does not get the terminal's signals, so
        # stop it explicitly.
        kill_process_tree(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def missing_cwd_result(cwd: str) -> Optional[RunResult]:
    """A RunResult describing a missing working directory, or None if it exists.

    Checked before spawning so a bad cwd is not misreported as a missing
    binary (subprocess raises FileNotFoundError for both).
    """
    if Path(cwd).is_dir():
        return None
    return RunResult(
        output="",
        success=False,
        error=f"Runner working directory does not exist: {cwd}",
    )


def _expand_env_value(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], value)
    if value.startswith("$") and len(value) > 1:
        return os.environ.get(value[1:], value)
    return value
