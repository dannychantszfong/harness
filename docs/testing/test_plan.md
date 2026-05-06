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
- HarnessConfig JSON loading and weight validation
- **All 3 coding-agent runners** — error paths (missing binary, missing package)
- Runner factory (`create_runner`) with each `RunnerType`
- `RunResult` fields are correctly populated when the runner exposes them (SDK transport)
- `RunResult.rate_limit_reset_at` is set when a subscription cap is hit
- `SubprocessRunner` — timeout, non-zero exit, binary not found, rate-limit detection
- `SDKRunner` — import error handling, model passthrough
- `CodexRunner` — local-provider routing flags

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
| RF-04 | `create_runner("invalid", config)` raises `ValueError` | Exception raised |
| RF-05 | `RunnerType.api_based()` returns `[]` (legacy method, kept for compat) | Empty list |
| RF-06 | `RunnerType` enum has no `ANTHROPIC` / `OPENAI` / `GEMINI` / `OPENROUTER` members | Sanity guard |

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

### 3.10 Rate-Limit Handling (subscription modes)

| ID | Test | Expected |
|----|------|----------|
| RL-01 | Stdout contains "You've hit your limit · resets 9:30pm (Europe/London)" → `rate_limit_reset_at` is a tz-aware UTC datetime | Field set, future datetime |
| RL-02 | Stated time is in the past relative to "now" (live parse) → next occurrence today; relative parse rolls to tomorrow | Correct rollover |
| RL-03 | Unparseable IANA zone (e.g. `Made/Up`) → returns `None` (no false trigger) | None |
| RL-04 | Trigger phrase absent (just "resets 9pm") → `_parse_reset_time` returns `None` | None |
| RL-05 | `_call_via_runner` raises `RunnerRateLimitedError` (not generic RuntimeError) when `rate_limit_reset_at` set | Typed exception |
| RL-06 | Orchestrator's `run()` catches the typed exception, prints the panel, schedules launchd if `auto_resume_on_rate_limit=True`, exits clean | No traceback, plist written |

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
# All three coding-agent runners
harness run tests/fixtures/simple_project/harness_config.json --runner subprocess
harness run tests/fixtures/simple_project/harness_config.json --runner sdk
harness run tests/fixtures/simple_project/harness_config.json --runner codex

# Verify each mode by setting env vars before the run:
ANTHROPIC_API_KEY=... harness run ... --runner subprocess  # Mode 2 (Claude API)
OPENAI_API_KEY=...    harness run ... --runner codex       # Mode 4 (OpenAI API)
ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1 \
ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY \
                      harness run ... --runner subprocess  # Mode 6 (OpenRouter)
```

For each run, verify:
- [ ] `features.json` created with all features PENDING
- [ ] `init.sh` created and executable
- [ ] First feature moves IN_PROGRESS, then PASSING or FAILING
- [ ] Sprint contract saved to `features.json`
- [ ] `progress.md` updated with correct percentages
- [ ] Git commit exists for each PASSING feature
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
