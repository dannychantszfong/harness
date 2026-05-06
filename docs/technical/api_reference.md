# API Reference — Claude Agent Harness

**Version:** 2.0  
**Last updated:** 2026-05-03  

---

## CLI Commands

### `harness run <config_file>`

Run the full harness end-to-end (Initialize → Plan → Feature Loop).

```bash
# Prompted to select runner interactively
harness run examples/web_app.yaml

# Skip prompt with --runner / -r
harness run examples/web_app.yaml --runner subprocess
harness run examples/web_app.yaml -r sdk
harness run examples/web_app.yaml -r openrouter
```

**Arguments:**

| Argument | Type | Description |
|----------|------|-------------|
| `config_file` | path | YAML config file (must exist) |

**Options:**

| Option | Short | Choices | Description |
|--------|-------|---------|-------------|
| `--runner` | `-r` | See below | Runner to use — skips interactive prompt |

**Runner choices:** `subprocess` `sdk` `codex` `anthropic` `openai` `gemini` `openrouter`

**Runner selection priority:** `--runner` flag > `code_runner` in config > interactive prompt

---

### `harness runners`

List all available runners in a formatted table. Does not start a run.

```bash
harness runners
```

```
┏━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Runner     ┃ Family  ┃ Billing           ┃ File I/O     ┃ Requires         ┃
┡━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ subprocess │ Agentic │ Claude sub        │ ✅ full      │ claude CLI       │
│ sdk        │ Agentic │ Claude sub        │ ✅ full      │ claude-code-sdk  │
│ codex      │ Agentic │ OpenAI sub        │ ✅ full      │ codex CLI        │
│ anthropic  │ API     │ Pay-per-token     │ ❌ text only │ ANTHROPIC_API_KEY│
│ openai     │ API     │ Pay-per-token     │ ❌ text only │ OPENAI_API_KEY   │
│ gemini     │ API     │ Pay-per-token     │ ❌ text only │ GEMINI_API_KEY   │
│ openrouter │ API     │ Pay-per-token     │ ❌ text only │ OPENROUTER_...   │
└────────────┴─────────┴───────────────────┴──────────────┴──────────────────┘
```

---

### `harness status <config_file>`

Print current project progress. No API calls.

```bash
harness status examples/web_app.yaml
```

---

### `harness init <config_file> <brief>`

Run only the initialization phase.

```bash
harness init examples/web_app.yaml "A todo app with Kanban view"
```

**Options:** `--project-name TEXT`

---

### `harness plan <config_file>`

Run only the planner agent to expand the brief into a spec.

```bash
harness plan examples/web_app.yaml
```

---

## Python API

### `HarnessConfig`

```python
from harness.config import HarnessConfig

config = HarnessConfig.from_yaml("examples/web_app.yaml")
```

**Config fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `project_name` | str | required | Project identifier |
| `brief` | str | required | 1–4 sentence project description |
| `output_dir` | str | `"./output"` | Where files are written |
| `code_runner` | str\|None | `None` | Runner to use (prompts if None) |
| `planner_model` | str | `"claude-opus-4-7"` | Model for PlannerAgent (Anthropic API) |
| `generator_model` | str | `"claude-opus-4-7"` | Model for API-based runners |
| `evaluator_model` | str | `"claude-opus-4-7"` | Model for EvaluatorAgent (Anthropic API) |
| `openai_api_key` | str\|None | `None` | Fallback if `OPENAI_API_KEY` not set |
| `gemini_api_key` | str\|None | `None` | Fallback if `GEMINI_API_KEY` not set |
| `openrouter_api_key` | str\|None | `None` | Fallback if `OPENROUTER_API_KEY` not set |
| `max_iterations_per_feature` | int | `15` | Max GAN loop iterations |
| `evaluator_pass_score` | float | `8.0` | Minimum score to mark PASSING |
| `context_reset_threshold_tokens` | int | `150_000` | Token count triggering a reset |
| `sprint_contract_enabled` | bool | `True` | Negotiate contracts before implementation |
| `evaluator_weights.design_quality` | float | `0.30` | |
| `evaluator_weights.originality` | float | `0.30` | |
| `evaluator_weights.craft` | float | `0.25` | |
| `evaluator_weights.functionality` | float | `0.15` | |

---

### `Orchestrator`

```python
from harness import Orchestrator, HarnessConfig
from harness.runners import RunnerType

config = HarnessConfig.from_yaml("examples/web_app.yaml")

# Runner from config/prompt
orchestrator = Orchestrator(config)

# Explicit runner
orchestrator = Orchestrator(config, runner_type=RunnerType.SUBPROCESS)
orchestrator = Orchestrator(config, runner_type=RunnerType.OPENROUTER)

orchestrator.run()
```

---

### `CodeRunner` and `RunResult`

```python
from harness.runners import create_runner, RunnerType, RunResult

runner = create_runner(RunnerType.SUBPROCESS, config)

result: RunResult = runner.implement(
    prompt="Implement the login feature...",
    cwd="/path/to/project",
    timeout_seconds=600,
)

print(result.success)          # bool
print(result.output)           # self-evaluation text
print(result.error)            # None if success
print(result.input_tokens)     # int | None (None for agentic runners)
print(result.output_tokens)    # int | None
print(result.cost_usd)         # float | None
print(result.tool_calls_observed)  # list[str] (SDK runner only)
```

---

### Using runners directly

```python
from harness.runners import SubprocessRunner, SDKRunner, OpenRouterAPIRunner

# Claude Code CLI
runner = SubprocessRunner(config)
result = runner.implement(prompt, cwd="/my/project")

# OpenRouter with a custom model
config.generator_model = "meta-llama/llama-3-70b-instruct"
runner = OpenRouterAPIRunner(config)
result = runner.implement(prompt, cwd="/my/project")
```

---

## YAML Config Reference

```yaml
# ── Required ─────────────────────────────────────────────────────────────────
project_name: "my-web-app"
brief: >
  A task management web app where users can create projects, add tasks
  with due dates and priorities, mark tasks complete, and view a Kanban board.

# ── Output ───────────────────────────────────────────────────────────────────
output_dir: "./output/web_app"

# ── Runner ───────────────────────────────────────────────────────────────────
# Leave null to be prompted. Options:
#   subprocess | sdk | codex         (agentic, subscription)
#   anthropic | openai | gemini | openrouter  (API, pay-per-token)
code_runner: null

# ── Provider API keys (prefer env vars over config) ──────────────────────────
openai_api_key: null       # or set OPENAI_API_KEY
gemini_api_key: null       # or set GEMINI_API_KEY
openrouter_api_key: null   # or set OPENROUTER_API_KEY

# ── Model selection ──────────────────────────────────────────────────────────
# Planner and Evaluator always use the Anthropic API (claude-opus-4-7).
# generator_model controls API runners. Ignored for agentic runners.
planner_model: "claude-opus-4-7"
generator_model: "claude-opus-4-7"   # or "gpt-4o", "gemini-2.5-pro", "anthropic/claude-opus-4-7"
evaluator_model: "claude-opus-4-7"

# ── GAN loop ─────────────────────────────────────────────────────────────────
max_iterations_per_feature: 15
evaluator_pass_score: 8.0

evaluator_weights:
  design_quality: 0.30
  originality: 0.30
  craft: 0.25
  functionality: 0.15

# ── Context management ───────────────────────────────────────────────────────
context_reset_threshold_tokens: 150000
sprint_contract_enabled: true
```

---

## Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | `anthropic` runner, Planner, Evaluator | Anthropic API key |
| `OPENAI_API_KEY` | `openai` runner | OpenAI API key |
| `GEMINI_API_KEY` | `gemini` runner | Google Gemini API key |
| `OPENROUTER_API_KEY` | `openrouter` runner | OpenRouter API key |

Env vars take precedence over values set in the YAML config.
