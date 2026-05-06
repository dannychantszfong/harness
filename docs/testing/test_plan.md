# Test Plan — Claude Agent Harness

**Version:** 2.0  
**Last updated:** 2026-05-03  
**Scope:** All harness components excluding generated application code  

---

## 1. Testing Strategy

| Level | Scope | API calls | Location |
|-------|-------|-----------|----------|
| Unit | Individual classes/methods | Mocked | `tests/` |
| Integration | Component interactions | Mocked | `tests/` |
| Runner smoke test | Each runner's error paths | Mocked binary/SDK | `tests/` |
| End-to-end | Full harness run | Real (expensive, manual) | Manual / CI gate |

**Environment:** Always run inside the `harness` conda environment.

```bash
conda activate harness
pytest tests/ -v
```

---

## 2. Test Scope

### In Scope
- Feature / ProjectProgress model validation and computed properties
- ProgressTracker CRUD operations
- HandoffDocument save/load/render
- ContextReset threshold detection and handoff construction
- SessionOpener context block generation
- Orchestrator phase sequencing, GAN loop count, max-iteration guard
- HarnessConfig YAML loading and weight validation
- **All 7 runners** — error paths (missing binary, missing key, missing package)
- Runner factory (`create_runner`) with each `RunnerType`
- `RunResult` fields are correctly populated for API runners
- `SubprocessRunner` — timeout, non-zero exit code, binary not found
- `SDKRunner` — import error handling
- `OpenRouterAPIRunner` — missing key, missing openai package

### Out of Scope
- Quality of LLM outputs (non-deterministic)
- Generated application code
- Real Playwright browser automation
- Real API calls (all mocked in unit/integration tests)

---

## 3. Test Cases by Component

### 3.1 Feature Model
*(unchanged — PM-01 through PM-10, see test_plan v1.0)*

### 3.2 Progress Tracker
*(unchanged — PT-01 through PT-09)*

### 3.3 Handoff Document
*(unchanged — HD-01 through HD-07)*

### 3.4 Context Reset
*(unchanged — CR-01 through CR-07)*

### 3.5 Orchestrator
*(unchanged — OR-01 through OR-05)*

---

### 3.6 Runner Factory

| ID | Test | Expected |
|----|------|----------|
| RF-01 | `create_runner(RunnerType.SUBPROCESS, config)` returns `SubprocessRunner` | Correct type |
| RF-02 | `create_runner(RunnerType.SDK, config)` returns `SDKRunner` | Correct type |
| RF-03 | `create_runner(RunnerType.CODEX, config)` returns `CodexRunner` | Correct type |
| RF-04 | `create_runner(RunnerType.ANTHROPIC, config)` returns `APIRunner` | Correct type |
| RF-05 | `create_runner(RunnerType.OPENAI, config)` returns `OpenAIAPIRunner` | Correct type |
| RF-06 | `create_runner(RunnerType.GEMINI, config)` returns `GeminiAPIRunner` | Correct type |
| RF-07 | `create_runner(RunnerType.OPENROUTER, config)` returns `OpenRouterAPIRunner` | Correct type |
| RF-08 | `create_runner("invalid", config)` raises `ValueError` | Exception raised |

---

### 3.7 SubprocessRunner

| ID | Test | Expected |
|----|------|----------|
| SR-01 | `claude` binary not on PATH → `RunResult(success=False, error=...)` | Error with install URL |
| SR-02 | `claude` exits with non-zero code → `RunResult(success=False)` | stderr in error field |
| SR-03 | Timeout exceeded → `RunResult(success=False, error="timed out")` | Timeout message |
| SR-04 | Successful run → `RunResult(success=True, output=...)` | Output populated |
| SR-05 | `input_tokens` / `cost_usd` are `None` (subscription runner) | Both None |

---

### 3.8 SDKRunner

| ID | Test | Expected |
|----|------|----------|
| SD-01 | `claude_code_sdk` not installed → `RunResult(success=False, error="pip install...")` | Error with pip command |
| SD-02 | SDK raises exception → `RunResult(success=False, error=str(exc))` | Error propagated |
| SD-03 | Successful run → `RunResult(success=True, output=..., tool_calls_observed=[...])` | Output + tool list |

---

### 3.9 CodexRunner

| ID | Test | Expected |
|----|------|----------|
| CX-01 | `codex` binary not on PATH → `RunResult(success=False, error=...)` | Error with npm install |
| CX-02 | `codex` exits non-zero → `RunResult(success=False)` | Error populated |
| CX-03 | Successful run → `RunResult(success=True, output=...)` | Output populated |

---

### 3.10 API Runners (Anthropic, OpenAI, Gemini, OpenRouter)

| ID | Test | Expected |
|----|------|----------|
| AR-01 | Missing API key (env not set, config None) → `RunResult(success=False)` | Error with key name |
| AR-02 | Missing package (openai not installed) → `RunResult(success=False, error="pip install")` | Error with command |
| AR-03 | Successful call → `RunResult(success=True, input_tokens>0, output_tokens>0)` | Tokens populated |
| AR-04 | `cost_usd` populated for anthropic/openai/gemini runners | Float value |
| AR-05 | `cost_usd` is None for openrouter runner | None (variable pricing) |
| AR-06 | `generator_model` in config is passed to provider SDK | Mock confirms model ID |

---

### 3.11 GeneratorAgent with Runner

| ID | Test | Expected |
|----|------|----------|
| GA-01 | `implement_feature` calls `runner.implement(prompt, cwd)` | Mock called once |
| GA-02 | Failed `RunResult` propagates error text in return value | Error in output |
| GA-03 | Token counts from `RunResult` accumulated into `agent.usage` | `usage.input_tokens` updated |
| GA-04 | Sprint contract negotiation still uses Anthropic API (not runner) | API mock called, runner mock not called |

---

### 3.12 Runner Selection (CLI + Config)

| ID | Test | Expected |
|----|------|----------|
| RS-01 | `--runner subprocess` flag creates `SubprocessRunner` | Correct type in orchestrator |
| RS-02 | `code_runner: sdk` in config creates `SDKRunner` | Skips prompt |
| RS-03 | No flag, no config → interactive prompt shown | `click.prompt` called |
| RS-04 | `--runner` flag overrides `code_runner` in config | Flag wins |

---

## 4. Test Execution

```bash
conda activate harness

# All tests
pytest tests/ -v

# Specific file
pytest tests/test_runners.py -v

# Coverage
pytest tests/ --cov=harness --cov-report=term-missing

# Short output
pytest tests/ -v --tb=short
```

---

## 5. End-to-End Test Checklist (Manual)

Run with a simple 3-feature project, each runner in turn:

```bash
# Agentic
harness run tests/fixtures/simple_project.yaml --runner subprocess
harness run tests/fixtures/simple_project.yaml --runner sdk

# API
harness run tests/fixtures/simple_project.yaml --runner anthropic
harness run tests/fixtures/simple_project.yaml --runner openai
```

For each run, verify:
- [ ] `features.json` created with all features PENDING
- [ ] `init.sh` created and executable
- [ ] First feature moves IN_PROGRESS, then PASSING or FAILING
- [ ] Sprint contract saved to `features.json`
- [ ] `progress.md` updated with correct percentages
- [ ] Git commit exists for each PASSING feature (agentic runners only)
- [ ] Restarting the run picks up from where it stopped

---

## 6. Quality Gates

| Gate | Threshold | Blocks |
|------|-----------|--------|
| Unit tests pass | 100% | PR merge |
| Test coverage (harness/) | ≥ 85% | PR merge |
| `mypy` type check | 0 errors | PR merge |
| `ruff` lint | 0 warnings | PR merge |
| E2E test (subprocess runner) | All phases complete | Release |
