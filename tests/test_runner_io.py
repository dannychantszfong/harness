"""Runner I/O contract: stdin prompts, structured output, timeouts, errors.

Covers P-10 (prompt via stdin), P-11/P-13/P-16 (SDK result/timeout/turns),
P-12 (missing cwd), P-14 (conservative rate-limit hints), P-15 (configurable
timeout) and C-01 steps 1-3 (token usage from every runner). External
binaries and the SDK are faked — nothing real is spawned.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.config import HarnessConfig
from harness.runners._rate_limit import failure_haystack, looks_rate_limited
from harness.runners.codex_runner import CodexRunner, parse_codex_events
from harness.runners.sdk_runner import SDKRunner
from harness.runners.subprocess_runner import SubprocessRunner, parse_claude_json


@pytest.fixture
def cfg(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path))


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _capture_run(monkeypatch, completed, binary="/usr/bin/tool"):
    captured: dict = {}

    def fake_run(args, *a, **k):
        captured["args"] = list(args)
        captured["kwargs"] = k
        return completed

    monkeypatch.setattr("shutil.which", lambda _: binary)
    monkeypatch.setattr("subprocess.run", fake_run)
    return captured


def _claude_result(**overrides) -> str:
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Implemented the feature.",
        "session_id": "sess-123",
        "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 5000,
            "output_tokens": 300,
        },
    }
    data.update(overrides)
    return json.dumps(data)


# ── P-10: the prompt travels through stdin ──────────────────────────────────

def test_subprocess_prompt_goes_through_stdin_not_argv(cfg, tmp_path, monkeypatch):
    prompt = "-starts-with-a-dash " + "x" * 250_000  # > 128 KiB single-arg limit
    captured = _capture_run(monkeypatch, _completed(stdout=_claude_result()))

    result = SubprocessRunner(cfg).implement(prompt, cwd=str(tmp_path))

    assert result.success is True
    assert captured["kwargs"]["input"] == prompt
    assert prompt not in captured["args"]
    assert all(len(arg) < 1000 for arg in captured["args"])
    assert captured["args"][1:5] == ["--print", "--output-format", "json", "--dangerously-skip-permissions"]


def test_codex_prompt_goes_through_stdin(cfg, tmp_path, monkeypatch):
    prompt = "-x " + "y" * 250_000
    captured = _capture_run(monkeypatch, _completed(stdout=""), binary="/usr/bin/codex")

    CodexRunner(cfg).implement(prompt, cwd=str(tmp_path))

    assert captured["kwargs"]["input"] == prompt
    assert captured["args"][-1] == "-"  # read instructions from stdin
    assert prompt not in captured["args"]


def test_subprocess_oserror_is_reported_not_raised(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", MagicMock(side_effect=OSError(7, "Argument list too long")))

    result = SubprocessRunner(cfg).implement("p", cwd=str(tmp_path))

    assert result.success is False
    assert "Could not start `claude`" in result.error


def test_runner_launches_resolved_binary_path(cfg, tmp_path, monkeypatch):
    """npm installs CLIs as .cmd shims on Windows; launch the resolved path."""
    captured = _capture_run(
        monkeypatch, _completed(stdout=""), binary=r"C:\npm\codex.CMD"
    )
    CodexRunner(cfg).implement("p", cwd=str(tmp_path))
    assert captured["args"][0] == r"C:\npm\codex.CMD"


# ── P-12: a missing cwd is not a missing binary ─────────────────────────────

@pytest.mark.parametrize("runner_cls", [SubprocessRunner, CodexRunner])
def test_missing_cwd_is_reported_as_missing_directory(cfg, tmp_path, monkeypatch, runner_cls):
    run = MagicMock()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr("subprocess.run", run)

    result = runner_cls(cfg).implement("p", cwd=str(tmp_path / "does-not-exist"))

    assert result.success is False
    assert "working directory does not exist" in result.error
    assert "binary" not in result.error
    run.assert_not_called()


def test_sdk_missing_cwd_is_reported(cfg, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_code_sdk", MagicMock())
    result = SDKRunner(cfg).implement("p", cwd=str(tmp_path / "nope"))
    assert result.success is False
    assert "working directory does not exist" in result.error


# ── P-15: timeout comes from config ─────────────────────────────────────────

def test_runner_timeout_defaults_to_config(cfg, tmp_path, monkeypatch):
    assert cfg.runner_timeout_seconds == 1800
    cfg.runner_timeout_seconds = 2400
    captured = _capture_run(monkeypatch, _completed(stdout=_claude_result()))

    SubprocessRunner(cfg).implement("p", cwd=str(tmp_path))
    assert captured["kwargs"]["timeout"] == 2400

    SubprocessRunner(cfg).implement("p", cwd=str(tmp_path), timeout_seconds=90)
    assert captured["kwargs"]["timeout"] == 90


@pytest.mark.parametrize("runner_cls", [SubprocessRunner, CodexRunner])
def test_timeout_sets_timed_out_flag(cfg, tmp_path, monkeypatch, runner_cls):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="tool", timeout=5)),
    )
    result = runner_cls(cfg).implement("p", cwd=str(tmp_path), timeout_seconds=5)
    assert result.success is False
    assert result.timed_out is True
    assert "timed out after 5s" in result.error


# ── C-01 step 1: claude --output-format json ────────────────────────────────

def test_subprocess_parses_json_usage_cost_and_result(cfg, tmp_path, monkeypatch):
    _capture_run(monkeypatch, _completed(stdout=_claude_result()))

    result = SubprocessRunner(cfg).implement("p", cwd=str(tmp_path))

    assert result.success is True
    assert result.output == "Implemented the feature."
    # new prompt tokens = uncached input + cache writes; reads tracked apart
    assert result.input_tokens == 1010
    assert result.cache_read_tokens == 5000
    assert result.output_tokens == 300
    assert result.cost_usd == pytest.approx(0.42)
    assert result.session_id == "sess-123"


def test_subprocess_is_error_result_is_a_failure(cfg, tmp_path, monkeypatch):
    _capture_run(
        monkeypatch,
        _completed(stdout=_claude_result(subtype="error_max_turns", is_error=True, result="ran out")),
    )
    result = SubprocessRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.success is False
    assert "error_max_turns" in result.error
    assert result.rate_limited is False


def test_subprocess_json_usage_limit_is_rate_limited(cfg, tmp_path, monkeypatch):
    _capture_run(
        monkeypatch,
        _completed(
            returncode=1,
            stdout=_claude_result(
                is_error=True,
                result="You've hit your limit · resets 9:30pm (Europe/London)",
            ),
        ),
    )
    result = SubprocessRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.success is False
    assert result.rate_limited is True
    assert result.rate_limit_reset_at is not None


def test_parse_claude_json_tolerates_verbose_array_and_log_lines():
    array = json.dumps([{"type": "system"}, {"type": "assistant"}, json.loads(_claude_result())])
    assert parse_claude_json(array)["result"] == "Implemented the feature."
    noisy = "some warning line\n" + _claude_result(result="ok")
    assert parse_claude_json(noisy)["result"] == "ok"
    assert parse_claude_json("plain text, not json") is None


# ── C-01 step 3: codex --json + --output-last-message ───────────────────────

_CODEX_EVENTS = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "thread.started", "thread_id": "thread-9"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "intermediate"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "final answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 19690, "cached_input_tokens": 12416, "output_tokens": 5}},
    )
)


def test_codex_parses_event_stream_usage(cfg, tmp_path, monkeypatch):
    _capture_run(monkeypatch, _completed(stdout=_CODEX_EVENTS), binary="/usr/bin/codex")

    result = CodexRunner(cfg).implement("p", cwd=str(tmp_path))

    assert result.success is True
    assert result.output == "final answer"
    assert result.input_tokens == 19690 - 12416
    assert result.cache_read_tokens == 12416
    assert result.output_tokens == 5
    assert result.session_id == "thread-9"


def test_codex_prefers_last_message_file(cfg, tmp_path, monkeypatch):
    def fake_run(args, *a, **k):
        out = args[args.index("--output-last-message") + 1]
        Path(out).write_text("from the file", encoding="utf-8")
        return _completed(stdout=_CODEX_EVENTS)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = CodexRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.output == "from the file"


def test_codex_turn_failed_is_a_failure():
    events = parse_codex_events(json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}))
    assert events.errors == ["429 Too Many Requests"]


def test_codex_turn_failed_marks_rate_limit(cfg, tmp_path, monkeypatch):
    stdout = json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}})
    _capture_run(monkeypatch, _completed(returncode=1, stdout=stdout), binary="/usr/bin/codex")
    result = CodexRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.success is False
    assert result.rate_limited is True
    assert "usage limit" in result.error


# ── P-14: rate-limit hints are conservative ─────────────────────────────────

@pytest.mark.parametrize("text", [
    "HTTP 429: Too Many Requests",
    "rate_limit_error: slow down",
    "Rate limited by provider",
    "quota exceeded for this billing period",
    "You have hit your limit",
    "usage cap reached",
])
def test_rate_limit_positive(text):
    assert looks_rate_limited(text) is True


@pytest.mark.parametrize("text", [
    "listening on port 14290",
    "request id 4291 failed with 500",
    "storage quota is 10GB",
    "implemented RateLimiter class with tests",
    "error: tests failed (2 of 30)",
])
def test_rate_limit_negative(text):
    assert looks_rate_limited(text) is False


def test_rate_limit_only_considers_stdout_tail():
    # A generator that *implements* a rate limiter prints about it early in
    # its transcript; the CLI's real failure reason is at the end.
    transcript = "\n".join(
        ["Implementing the rate limit middleware (HTTP 429 on overflow)"]
        + [f"step {i}" for i in range(40)]
        + ["Error: tests failed"]
    )
    assert looks_rate_limited(failure_haystack(transcript, "")) is False
    assert looks_rate_limited(failure_haystack(transcript, "429 Too Many Requests")) is True


def test_subprocess_rate_limiter_transcript_is_not_a_cap(cfg, tmp_path, monkeypatch):
    stdout = "\n".join(
        ["Added rate limit handling returning 429"] + [f"line {i}" for i in range(30)] + ["build failed"]
    )
    _capture_run(monkeypatch, _completed(returncode=1, stdout=stdout, stderr="exit 1"))
    result = SubprocessRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.success is False
    assert result.rate_limited is False


# ── P-11 / P-13 / P-16: SDK runner ──────────────────────────────────────────

class _Text:
    def __init__(self, text):
        self.text = text


class _ToolUse:
    def __init__(self, name):
        self.name = name


class AssistantMessage:
    def __init__(self, *blocks):
        self.content = list(blocks)


class ResultMessage:
    """Shape of claude_code_sdk 0.0.25's ResultMessage."""

    def __init__(self, **kw):
        self.subtype = kw.get("subtype", "success")
        self.duration_ms = 10
        self.duration_api_ms = 8
        self.is_error = kw.get("is_error", False)
        self.num_turns = 3
        self.session_id = "sdk-sess"
        self.total_cost_usd = kw.get("total_cost_usd", 0.12)
        self.usage = kw.get("usage", {
            "input_tokens": 20,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 4000,
            "output_tokens": 70,
        })
        self.result = kw.get("result", "FINAL MESSAGE")


def _install_fake_sdk(monkeypatch, messages=None, query=None):
    sdk = MagicMock()
    if query is None:
        async def query(*args, **kwargs):
            for m in messages:
                yield m
    sdk.query = query
    sdk.ClaudeCodeOptions = MagicMock()
    monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk)
    return sdk


def test_sdk_reads_realistic_result_message(cfg, tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch, [
        AssistantMessage(_Text("thinking out loud"), _ToolUse("Write")),
        AssistantMessage(_Text("more chatter")),
        ResultMessage(),
    ])

    result = SDKRunner(cfg).implement("p", cwd=str(tmp_path))

    assert result.success is True
    assert result.output == "FINAL MESSAGE"  # result, not concatenated chatter
    assert result.input_tokens == 220
    assert result.cache_read_tokens == 4000
    assert result.output_tokens == 70
    assert result.cost_usd == pytest.approx(0.12)
    assert result.tool_calls_observed == ["Write"]


def test_sdk_is_error_result_is_a_failure(cfg, tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch, [ResultMessage(subtype="error_max_turns", is_error=True, result=None)])
    result = SDKRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.success is False
    assert "error_max_turns" in result.error


def test_sdk_falls_back_to_concatenated_text_without_result(cfg, tmp_path, monkeypatch):
    _install_fake_sdk(monkeypatch, [AssistantMessage(_Text("a")), AssistantMessage(_Text("b")), ResultMessage(result=None)])
    result = SDKRunner(cfg).implement("p", cwd=str(tmp_path))
    assert result.output == "a\nb"


def test_sdk_max_turns_from_config(cfg, tmp_path, monkeypatch):
    assert cfg.sdk_max_turns == 100
    cfg.sdk_max_turns = 37
    sdk = _install_fake_sdk(monkeypatch, [ResultMessage()])
    SDKRunner(cfg).implement("p", cwd=str(tmp_path))
    assert sdk.ClaudeCodeOptions.call_args.kwargs["max_turns"] == 37


def test_sdk_timeout_has_a_message_and_closes_the_stream(cfg, tmp_path, monkeypatch):
    closed = {"value": False}

    class SlowStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            raise StopAsyncIteration

        async def aclose(self):
            closed["value"] = True

    _install_fake_sdk(monkeypatch, query=lambda *a, **k: SlowStream())

    result = SDKRunner(cfg).implement("p", cwd=str(tmp_path), timeout_seconds=1)

    assert result.success is False
    assert result.timed_out is True
    assert result.error == "Claude Code SDK timed out after 1s"
    assert closed["value"] is True  # transport cleanup → subprocess terminated


def test_sdk_preflight_mentions_frozen_package(cfg, monkeypatch):
    sdk = MagicMock()
    sdk.__version__ = "0.0.25"
    monkeypatch.setitem(sys.modules, "claude_code_sdk", sdk)
    pf = SDKRunner(cfg).preflight()
    assert pf.ok is True
    assert "frozen claude-code-sdk" in pf.details
    assert pf.warning is None

    sdk.__version__ = "0.0.99"
    assert "0.0.25" in SDKRunner(cfg).preflight().warning
