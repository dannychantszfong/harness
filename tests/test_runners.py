"""Tests for the runner layer: factory, SubprocessRunner, SDKRunner, API runners.

All external binaries and API clients are mocked — no real API calls are made.
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
from harness.runners.api_runner import APIRunner
from harness.runners.openai_api_runner import OpenAIAPIRunner
from harness.runners.gemini_api_runner import GeminiAPIRunner
from harness.runners.openrouter_api_runner import OpenRouterAPIRunner


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
    (RunnerType.ANTHROPIC,   APIRunner),
    (RunnerType.OPENAI,      OpenAIAPIRunner),
    (RunnerType.GEMINI,      GeminiAPIRunner),
    (RunnerType.OPENROUTER,  OpenRouterAPIRunner),
])
def test_create_runner_returns_correct_type(runner_type, expected_cls, tmp_config):
    runner = create_runner(runner_type, tmp_config)
    assert isinstance(runner, expected_cls)


def test_create_runner_invalid_raises(tmp_config):
    with pytest.raises((ValueError, KeyError)):
        create_runner("invalid_runner", tmp_config)  # type: ignore[arg-type]


# ── RunnerType helpers ────────────────────────────────────────────────────────

def test_runner_type_agentic_family():
    agentic = RunnerType.agentic()
    assert RunnerType.SUBPROCESS in agentic
    assert RunnerType.SDK in agentic
    assert RunnerType.CODEX in agentic
    assert RunnerType.ANTHROPIC not in agentic


def test_runner_type_api_family():
    api = RunnerType.api_based()
    assert RunnerType.ANTHROPIC in api
    assert RunnerType.OPENAI in api
    assert RunnerType.GEMINI in api
    assert RunnerType.OPENROUTER in api
    assert RunnerType.SUBPROCESS not in api


def test_runner_type_choices_has_all_seven():
    assert len(RunnerType.choices()) == 7


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


# ── APIRunner (Anthropic) ─────────────────────────────────────────────────────

def test_anthropic_runner_returns_tokens(tmp_config, monkeypatch):
    mock_stream = MagicMock()
    mock_stream.__enter__ = lambda s: s
    mock_stream.__exit__ = MagicMock(return_value=False)
    mock_stream.text_stream = iter(["hello ", "world"])
    mock_usage = MagicMock(input_tokens=1000, output_tokens=500)
    mock_stream.get_final_message.return_value = MagicMock(usage=mock_usage)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.stream.return_value = mock_stream
        runner = APIRunner(tmp_config)
        result = runner.implement("prompt", cwd="/tmp")

    assert result.success is True
    assert result.input_tokens == 1000
    assert result.output_tokens == 500
    assert result.cost_usd is not None and result.cost_usd > 0


def test_anthropic_runner_cost_positive(tmp_config, monkeypatch):
    mock_stream = MagicMock()
    mock_stream.__enter__ = lambda s: s
    mock_stream.__exit__ = MagicMock(return_value=False)
    mock_stream.text_stream = iter(["output"])
    mock_usage = MagicMock(input_tokens=100, output_tokens=50)
    mock_stream.get_final_message.return_value = MagicMock(usage=mock_usage)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.stream.return_value = mock_stream
        runner = APIRunner(tmp_config)
        result = runner.implement("prompt", cwd="/tmp")

    # cost = (100*15 + 50*75) / 1_000_000 = 0.005250
    assert result.cost_usd == pytest.approx((100 * 15 + 50 * 75) / 1_000_000)


# ── OpenAI Runner ─────────────────────────────────────────────────────────────

def test_openai_missing_key(tmp_config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp_config.openai_api_key = None
    mock_openai = MagicMock()
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    runner = OpenAIAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "OPENAI_API_KEY" in result.error


def test_openai_missing_package(tmp_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setitem(sys.modules, "openai", None)
    runner = OpenAIAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "pip install openai" in result.error


def test_openai_success(tmp_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")

    mock_chunk = MagicMock()
    mock_chunk.choices = [MagicMock(delta=MagicMock(content="Hello world"))]

    mock_stream = MagicMock()
    mock_stream.__iter__ = MagicMock(return_value=iter([mock_chunk]))
    mock_stream.__enter__ = lambda s: s
    mock_stream.__exit__ = MagicMock(return_value=False)

    mock_openai = MagicMock()
    mock_openai.OpenAI.return_value.chat.completions.create.return_value = mock_stream
    monkeypatch.setitem(sys.modules, "openai", mock_openai)

    runner = OpenAIAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True


# ── Gemini Runner ─────────────────────────────────────────────────────────────

def test_gemini_missing_key(tmp_config, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    tmp_config.gemini_api_key = None
    mock_genai = MagicMock()
    monkeypatch.setitem(sys.modules, "google", MagicMock())
    monkeypatch.setitem(sys.modules, "google.generativeai", mock_genai)
    runner = GeminiAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "GEMINI_API_KEY" in result.error


def test_gemini_missing_package(tmp_config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake")
    monkeypatch.setitem(sys.modules, "google.generativeai", None)
    runner = GeminiAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "pip install" in result.error


# ── OpenRouter Runner ─────────────────────────────────────────────────────────

def test_openrouter_missing_key(tmp_config, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tmp_config.openrouter_api_key = None
    mock_openai = MagicMock()
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    runner = OpenRouterAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "OPENROUTER_API_KEY" in result.error


def test_openrouter_cost_is_none(tmp_config, monkeypatch):
    """OpenRouter cost is model-dependent — runner should return None."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")

    mock_chunk = MagicMock()
    mock_chunk.choices = [MagicMock(delta=MagicMock(content="output text"))]

    mock_stream = MagicMock()
    mock_stream.__iter__ = MagicMock(return_value=iter([mock_chunk]))
    mock_stream.__enter__ = lambda s: s
    mock_stream.__exit__ = MagicMock(return_value=False)

    mock_openai = MagicMock()
    mock_openai.OpenAI.return_value.chat.completions.create.return_value = mock_stream
    monkeypatch.setitem(sys.modules, "openai", mock_openai)

    runner = OpenRouterAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.cost_usd is None


# ── AR-01: parametrized missing-key check ─────────────────────────────────────

@pytest.mark.parametrize("runner_cls,env_var,config_attr,pkg", [
    (OpenAIAPIRunner,     "OPENAI_API_KEY",     "openai_api_key",     "openai"),
    (GeminiAPIRunner,     "GEMINI_API_KEY",      "gemini_api_key",     None),  # handled separately below
    (OpenRouterAPIRunner, "OPENROUTER_API_KEY",  "openrouter_api_key", "openai"),
])
def test_api_runner_missing_key_parametrized(runner_cls, env_var, config_attr, pkg, tmp_config, monkeypatch):
    monkeypatch.delenv(env_var, raising=False)
    setattr(tmp_config, config_attr, None)
    if pkg is not None:
        monkeypatch.setitem(sys.modules, pkg, MagicMock())
    else:
        # Gemini needs both google parent and submodule mocked
        monkeypatch.setitem(sys.modules, "google", MagicMock())
        monkeypatch.setitem(sys.modules, "google.generativeai", MagicMock())
    runner = runner_cls(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert env_var in result.error
