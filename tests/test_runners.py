"""Tests for the runner layer: factory + the three coding-agent runners.

External binaries are mocked — no real subprocess execution happens.
API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not standalone
runners; they plug into one of the three agentic runners via env vars.
"""

import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from harness.config import HarnessConfig
from harness.runners import create_runner, RunnerType
from harness.runners.base import RunResult
from harness.runners.subprocess_runner import SubprocessRunner
from harness.runners.sdk_runner import SDKRunner
from harness.runners.codex_runner import CodexRunner


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="test",
        brief="test brief",
        output_dir=str(tmp_path),
        generator_model="claude-opus-4-7",
    )


# ── Runner Factory ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("runner_type,expected_cls", [
    (RunnerType.SUBPROCESS,  SubprocessRunner),
    (RunnerType.SDK,         SDKRunner),
    (RunnerType.CODEX,       CodexRunner),
])
def test_create_runner_returns_correct_type(runner_type, expected_cls, tmp_config):
    runner = create_runner(runner_type, tmp_config)
    assert isinstance(runner, expected_cls)


def test_create_runner_invalid_raises(tmp_config):
    with pytest.raises((ValueError, KeyError)):
        create_runner("invalid_runner", tmp_config)  # type: ignore[arg-type]


# ── RunnerType ────────────────────────────────────────────────────────────────

def test_runner_type_agentic_family_includes_all_three():
    agentic = RunnerType.agentic()
    assert agentic == [RunnerType.SUBPROCESS, RunnerType.SDK, RunnerType.CODEX]


def test_runner_type_api_based_is_empty_after_refactor():
    """API providers are no longer standalone runners."""
    assert RunnerType.api_based() == []


def test_runner_type_choices_has_three():
    assert RunnerType.choices() == ["subprocess", "sdk", "codex"]


def test_runner_type_no_legacy_api_members():
    """Sanity guard against accidental re-introduction of API-runner members."""
    legacy_names = {"ANTHROPIC", "OPENAI", "GEMINI", "OPENROUTER"}
    assert legacy_names.isdisjoint({m.name for m in RunnerType})


# ── SubprocessRunner ──────────────────────────────────────────────────────────

def test_subprocess_missing_binary(tmp_config, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "claude" in result.error.lower()
    assert "install" in result.error.lower()


def test_subprocess_nonzero_exit(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "some error"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    # Error report now includes diagnostic context (exit code, stderr, prompt size)
    assert "exited with code 1" in result.error
    assert "some error" in result.error
    assert result.rate_limit_reset_at is None


def test_subprocess_rate_limit_detected(tmp_config, monkeypatch):
    """A 'You've hit your limit' message must be parsed into rate_limit_reset_at."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "You've hit your limit · resets 9:30pm (Europe/London)"
    mock_result.stderr = ""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert result.rate_limit_reset_at is not None
    assert result.rate_limit_reset_at.tzinfo is not None  # always tz-aware UTC
    assert result.rate_limited is True


def test_subprocess_rate_limited_flag_without_reset_time(tmp_config, monkeypatch):
    """A 429-style failure with no reset hint must still set rate_limited=True
    so the orchestrator's role-fallback path triggers, even when we can't
    schedule auto-resume."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "HTTP 429: too many requests, please slow down"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is None


def test_subprocess_timeout(tmp_config, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)),
    )
    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp", timeout_seconds=1)
    assert result.success is False
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


def test_subprocess_success(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "self-evaluation text"
    mock_result.stderr = ""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert result.output == "self-evaluation text"


def test_subprocess_passes_configured_model(tmp_config, monkeypatch):
    tmp_config.code_runner_model = "sonnet"
    captured = {}
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ok"
    mock_result.stderr = ""

    def fake_run(args, *a, **k):
        captured["args"] = args
        return mock_result

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", fake_run)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert "--model" in captured["args"]
    assert "sonnet" in captured["args"]


def test_subprocess_expands_profile_env(tmp_config, monkeypatch):
    tmp_config.active_runner_env = {
        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1",
        "ANTHROPIC_AUTH_TOKEN": "$OPENROUTER_API_KEY",
    }
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    captured = {}
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ok"
    mock_result.stderr = ""

    def fake_run(args, *a, **k):
        captured["env"] = k.get("env")
        return mock_result

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = SubprocessRunner(tmp_config).implement("prompt", cwd="/tmp")

    assert result.success is True
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-or-test"


def test_subprocess_no_token_data(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "self-evaluation text"
    mock_result.stderr = ""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.input_tokens is None
    assert result.cost_usd is None


# ── SDKRunner ─────────────────────────────────────────────────────────────────

def test_sdk_missing_package(tmp_config, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_code_sdk", None)
    runner = SDKRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "pip install" in result.error


def test_sdk_success(tmp_config, monkeypatch):
    """SDKRunner streams messages from claude_code_sdk.query()."""
    from types import SimpleNamespace

    # Assistant message with a tool block and a text block
    tool_block = SimpleNamespace(name="Write", text=None)
    text_block = SimpleNamespace(name=None, text="Self evaluation complete.")

    class AssistantMsg:
        content = [tool_block, text_block]

    # ResultMessage — type().__name__ must equal "ResultMessage"
    class ResultMessage:
        usage = SimpleNamespace(input_tokens=500, output_tokens=200)
        cost_usd = 0.05

    async def fake_query(*args, **kwargs):
        for msg in [AssistantMsg(), ResultMessage()]:
            yield msg

    mock_sdk = MagicMock()
    mock_sdk.query = fake_query
    mock_sdk.ClaudeCodeOptions = MagicMock()

    monkeypatch.setitem(sys.modules, "claude_code_sdk", mock_sdk)

    runner = SDKRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert "Write" in result.tool_calls_observed
    assert result.cost_usd == pytest.approx(0.05)


def test_sdk_passes_configured_model(tmp_config, monkeypatch):
    """Claude Code SDK accepts the coding-agent model in ClaudeCodeOptions."""
    tmp_config.code_runner_model = "opus"

    class ResultMessage:
        usage = None
        cost_usd = 0.0

    async def fake_query(*args, **kwargs):
        yield ResultMessage()

    mock_sdk = MagicMock()
    mock_sdk.query = fake_query
    mock_sdk.ClaudeCodeOptions = MagicMock()

    monkeypatch.setitem(sys.modules, "claude_code_sdk", mock_sdk)

    runner = SDKRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert mock_sdk.ClaudeCodeOptions.call_args.kwargs["model"] == "opus"


def test_sdk_runs_in_bypass_permissions_mode(tmp_config, monkeypatch):
    """SDK must match SubprocessRunner's --dangerously-skip-permissions YOLO.

    Without this, the SDK falls back to its default permission_mode and would
    either prompt or fail on the first risky tool call in unattended runs.
    """
    class ResultMessage:
        usage = None
        cost_usd = 0.0

    async def fake_query(*args, **kwargs):
        yield ResultMessage()

    mock_sdk = MagicMock()
    mock_sdk.query = fake_query
    mock_sdk.ClaudeCodeOptions = MagicMock()
    monkeypatch.setitem(sys.modules, "claude_code_sdk", mock_sdk)

    runner = SDKRunner(tmp_config)
    runner.implement("prompt", cwd="/tmp")

    assert (
        mock_sdk.ClaudeCodeOptions.call_args.kwargs["permission_mode"]
        == "bypassPermissions"
    )


def test_sdk_rate_limit_sets_rate_limited_flag(tmp_config, monkeypatch):
    """SDK exceptions matching the rate-limit hints must set rate_limited=True
    so the orchestrator rotates profiles. reset_at may be None — that's fine,
    rotation still triggers; only auto-resume scheduling needs reset_at."""
    async def fake_query(*args, **kwargs):
        if False:
            yield None
        raise RuntimeError("Anthropic API: 429 Too Many Requests — please slow down")

    mock_sdk = MagicMock()
    mock_sdk.query = fake_query
    mock_sdk.ClaudeCodeOptions = MagicMock()
    monkeypatch.setitem(sys.modules, "claude_code_sdk", mock_sdk)

    runner = SDKRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is None


def test_sdk_rate_limit_parses_reset_time_when_present(tmp_config, monkeypatch):
    """When a parseable reset hint happens to appear in the SDK error text,
    SDK runner populates rate_limit_reset_at the same way subprocess does."""
    async def fake_query(*args, **kwargs):
        if False:
            yield None
        raise RuntimeError(
            "claude_code_sdk: You've hit your limit · resets 9:30pm (Europe/London)"
        )

    mock_sdk = MagicMock()
    mock_sdk.query = fake_query
    mock_sdk.ClaudeCodeOptions = MagicMock()
    monkeypatch.setitem(sys.modules, "claude_code_sdk", mock_sdk)

    runner = SDKRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is not None
    assert result.rate_limit_reset_at.tzinfo is not None


# ── CodexRunner ───────────────────────────────────────────────────────────────

def test_codex_missing_binary(tmp_config, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    runner = CodexRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "codex" in result.error.lower()


def test_codex_nonzero_exit(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "codex error"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = CodexRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False


def test_codex_rate_limit_hint_sets_flag(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "429 rate limit exceeded"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    result = CodexRunner(tmp_config).implement("prompt", cwd="/tmp")

    assert result.success is False
    assert result.rate_limited is True
    # Codex 429 messages don't carry a parseable reset time — that's fine.
    # The rate_limited flag alone is enough to trigger profile rotation.
    assert result.rate_limit_reset_at is None


def test_codex_rate_limit_parses_reset_time_when_present(tmp_config, monkeypatch):
    """If codex's stdout/stderr happens to contain the strict reset pattern,
    we populate rate_limit_reset_at the same way subprocess + sdk do.
    Cross-runner parity for rate-limit handling is the contract."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "You've hit your limit · resets 9:30pm (Europe/London)"
    mock_result.stderr = ""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    result = CodexRunner(tmp_config).implement("prompt", cwd="/tmp")

    assert result.success is False
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is not None
    assert result.rate_limit_reset_at.tzinfo is not None


def test_codex_success(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "codex output"
    mock_result.stderr = ""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = CodexRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert result.output == "codex output"


def test_codex_passes_model_and_local_provider(tmp_config, monkeypatch):
    tmp_config.code_runner_model = "qwen2.5-coder"
    tmp_config.codex_local_provider = "ollama"
    captured = {}
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "codex output"
    mock_result.stderr = ""

    def fake_run(args, *a, **k):
        captured["args"] = args
        return mock_result

    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr("subprocess.run", fake_run)

    runner = CodexRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert captured["args"][:2] == ["codex", "exec"]
    assert "--model" in captured["args"]
    assert "qwen2.5-coder" in captured["args"]
    assert "--oss" in captured["args"]
    assert "--local-provider" in captured["args"]
    assert "ollama" in captured["args"]
