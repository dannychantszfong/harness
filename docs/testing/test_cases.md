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
    (RunnerType.ANTHROPIC,   APIRunner),
    (RunnerType.OPENAI,      OpenAIAPIRunner),
    (RunnerType.GEMINI,      GeminiAPIRunner),
    (RunnerType.OPENROUTER,  OpenRouterAPIRunner),
])
def test_create_runner_returns_correct_type(runner_type, expected_cls, tmp_config):
    runner = create_runner(runner_type, tmp_config)
    assert isinstance(runner, expected_cls)
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

## API Runner Tests

### AR-01: Missing API key returns actionable error

```python
@pytest.mark.parametrize("runner_cls,key_env,key_name", [
    (OpenAIAPIRunner,      "OPENAI_API_KEY",      "OPENAI_API_KEY"),
    (GeminiAPIRunner,      "GEMINI_API_KEY",       "GEMINI_API_KEY"),
    (OpenRouterAPIRunner,  "OPENROUTER_API_KEY",   "OPENROUTER_API_KEY"),
])
def test_api_runner_missing_key(runner_cls, key_env, key_name, tmp_config, monkeypatch):
    monkeypatch.delenv(key_env, raising=False)
    tmp_config.openai_api_key = None
    tmp_config.gemini_api_key = None
    tmp_config.openrouter_api_key = None

    runner = runner_cls(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert key_name in result.error
```

---

### AR-02: Missing provider package returns pip install error

```python
def test_openai_runner_missing_package(tmp_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setitem(sys.modules, "openai", None)

    runner = OpenAIAPIRunner(tmp_config)
    result = runner.implement("prompt", cwd="/tmp")
    assert result.success is False
    assert "pip install openai" in result.error
```

---

### AR-03 / AR-04: Successful call returns tokens and cost

```python
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
```

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

## Regression Checklist (v2.0 additions)

Before each release, verify:

- [ ] `harness runners` prints the full table without error
- [ ] `harness run config.yaml --runner subprocess` skips the prompt
- [ ] `harness run config.yaml --runner openrouter` uses OPENROUTER_API_KEY
- [ ] Missing binary for agentic runner shows clear install instructions
- [ ] Missing API key for API runner shows correct env var name
- [ ] `code_runner: sdk` in YAML skips the interactive prompt
- [ ] Switching runner mid-project (config change) resumes from last state
- [ ] `ANTHROPIC_API_KEY` is always required regardless of runner choice
