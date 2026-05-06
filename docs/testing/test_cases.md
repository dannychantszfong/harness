# Test Cases — Claude Agent Harness

**Version:** 2.0  
**Last updated:** 2026-05-03  

Detailed step-by-step test cases. Previous cases (PM-*, PT-*, HD-*, CR-*, OR-*) are unchanged — see v1.0. This document adds runner-related cases.

---

## Runner Factory Tests

### RF-01 through RF-07: create_runner returns correct type

```python
from harness.runners import create_runner, RunnerType
from harness.runners.subprocess_runner import SubprocessRunner
from harness.runners.sdk_runner import SDKRunner
# ... etc.

@pytest.mark.parametrize("runner_type,expected_cls", [
    (RunnerType.SUBPROCESS,  SubprocessRunner),
    (RunnerType.SDK,         SDKRunner),
    (RunnerType.CODEX,       CodexRunner),
])
def test_create_runner_returns_correct_type(runner_type, expected_cls, tmp_config):
    runner = create_runner(runner_type, tmp_config)
    assert isinstance(runner, expected_cls)


def test_runner_type_api_based_is_empty_after_refactor():
    """API providers are no longer standalone runners — they plug into
    the three coding-agent runners via env vars. The api_based() method
    is preserved (returns []) so existing callers don't crash."""
    assert RunnerType.api_based() == []
```

---

### RF-08: Unknown runner type raises ValueError

```python
def test_create_runner_invalid_raises(tmp_config):
    with pytest.raises(ValueError):
        create_runner("invalid_runner", tmp_config)
```

---

## SubprocessRunner Tests

### SR-01: Missing binary returns error RunResult

```python
def test_subprocess_missing_binary(tmp_config, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "claude" in result.error.lower()
    assert "install" in result.error.lower()
```

---

### SR-02: Non-zero exit code returns failure

```python
def test_subprocess_nonzero_exit(tmp_config, monkeypatch):
    import subprocess
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "some error"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert result.error == "some error"
```

---

### SR-03: Timeout raises and returns failure

```python
def test_subprocess_timeout(tmp_config, monkeypatch):
    import subprocess
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", MagicMock(
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)
    ))
    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp", timeout_seconds=1)
    assert result.success is False
    assert "timed out" in result.error.lower()
```

---

### SR-05: Tokens and cost are None (subscription runner)

```python
def test_subprocess_no_token_data(tmp_config, monkeypatch):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "self-evaluation text"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: mock_result)

    runner = SubprocessRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is True
    assert result.input_tokens is None
    assert result.cost_usd is None
```

---

## SDKRunner Tests

### SD-01: SDK not installed returns error

```python
def test_sdk_missing_package(tmp_config, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "claude_code_sdk",
        None  # simulate ImportError
    )
    runner = SDKRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "pip install" in result.error
```

---

## Rate-Limit Detection Tests (subscription modes)

### RL-01: Stdout signature populates rate_limit_reset_at

```python
def test_subprocess_rate_limit_detected(tmp_config, monkeypatch):
    """A 'You've hit your limit · resets …' message must be parsed into
    rate_limit_reset_at (tz-aware UTC datetime)."""
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
```

---

### RL-05: agents/base raises RunnerRateLimitedError, not RuntimeError

```python
def test_call_via_runner_raises_typed_exception_on_rate_limit(tmp_config):
    from datetime import datetime, timezone
    from harness.runners.base import RunnerRateLimitedError

    fake_runner = MagicMock()
    fake_runner.implement.return_value = RunResult(
        output="", success=False, error="hit limit",
        rate_limit_reset_at=datetime.now(timezone.utc),
    )
    agent = SomeAgent(tmp_config, runner=fake_runner)
    with pytest.raises(RunnerRateLimitedError):
        agent._call_via_runner("prompt")
```

---

### RL-06: Orchestrator handles rate limit gracefully

See [tests/test_auto_resume.py](../../tests/test_auto_resume.py) — the
end-to-end test asserts that:
1. `Orchestrator.run()` does NOT raise when a `RunnerRateLimitedError` is
   raised by an agent
2. A launchd plist is written to `~/Library/LaunchAgents/`
3. The wrapper script is executable
4. `auto_resume.cancel(project_id)` removes the plist

---

## GeneratorAgent + Runner Integration Tests

### GA-01: implement_feature delegates to runner.implement

```python
def test_generator_calls_runner(tmp_config, one_feature_progress):
    mock_runner = MagicMock()
    mock_runner.implement.return_value = RunResult(
        output="Self evaluation text", success=True
    )
    agent = GeneratorAgent(tmp_config, runner=mock_runner)
    feature = one_feature_progress.features[0]
    
    result = agent.implement_feature(
        feature=feature,
        progress=one_feature_progress,
        session_preamble="Context here",
    )
    
    mock_runner.implement.assert_called_once()
    assert "Self evaluation text" in result
```

---

### GA-04: Sprint contract uses API, not runner

```python
def test_sprint_contract_uses_api_not_runner(tmp_config, one_feature_progress):
    mock_runner = MagicMock()
    agent = GeneratorAgent(tmp_config, runner=mock_runner)
    feature = one_feature_progress.features[0]

    with patch.object(agent, "_call") as mock_call:
        mock_call.return_value = ("", [{"name": "propose_sprint_contract", 
            "input": {"acceptance_criteria": ["works"], "out_of_scope": []}}])
        agent.negotiate_sprint_contract(feature, spec="spec")

    # API was called, but runner was not
    mock_call.assert_called_once()
    mock_runner.implement.assert_not_called()
```

---

## Regression Checklist (v2.1 additions)

Before each release, verify:

- [ ] `harness runners` prints the three coding-agent rows without error
- [ ] `harness run harness_config.json --runner subprocess` skips the prompt
- [ ] Mode 6 (OpenRouter via Claude Code) works with `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`
- [ ] Missing binary for any of the three runners shows clear install instructions
- [ ] Missing env var for the chosen mode shows actionable guidance
- [ ] `"code_runner": "sdk"` in `harness_config.json` skips the interactive prompt
- [ ] Subscription rate-limit hit surfaces the friendly panel (no traceback)
- [ ] `auto_resume_on_rate_limit: true` (default) writes a launchd plist
- [ ] `harness import <repo>` correctly detects stage and routes to review-only when ≥80% features pass
- [ ] `harness new --github-repo owner/repo` keeps generated output out of the Harness repo and pushes the output project repo
- [ ] `harness import <repo> --git-remote URL` copies without source `.git/` and sets the copied project remote
- [ ] `harness setup` writes runner profiles and new projects inherit them
- [ ] A rate-limited generator profile falls through to the next allowed profile
- [ ] Switching runner mid-project (config change) resumes from last state
- [ ] `ANTHROPIC_API_KEY` is required for api orchestration, but not runner orchestration
- [ ] `--model` is persisted as `code_runner_model` for Claude Code/Codex runners
