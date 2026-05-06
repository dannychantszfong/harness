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
harness run examples/web_app.yaml -r codex
```

**Arguments:**

| Argument | Type | Description |
|----------|------|-------------|
| `config_file` | path | YAML config file (must exist) |

**Options:**

| Option | Short | Choices | Description |
|--------|-------|---------|-------------|
| `--runner` | `-r` | See below | Runner to use — skips interactive prompt |

**Runner choices:** `subprocess` `sdk` `codex`

API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not standalone runners — they plug into one of the three above via env vars (see Environment Variables below).

**Runner selection priority:** `--runner` flag > `code_runner` in config > interactive prompt

---

### `harness runners`

List the three coding-agent runners in a formatted table. Does not start a run.

```bash
harness runners
```

```
┏━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ Runner     ┃ Family  ┃ Billing             ┃ File I/O ┃ Requires             ┃
┡━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ subprocess │ Agentic │ Claude subscription │ ✅ full  │ `claude` CLI         │
│ sdk        │ Agentic │ Claude subscription │ ✅ full  │ pip install …        │
│ codex      │ Agentic │ OpenAI subscription │ ✅ full  │ `codex` CLI          │
└────────────┴─────────┴─────────────────────┴──────────┴──────────────────────┘
```

The four direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) plug into one of the three runners via env vars and are not separately listed.

---

### `harness animation-theme <theme>`

Ask a signed-in coding agent to rewrite the playful quiet-animation verbs in the local Harness checkout.

```bash
harness animation-theme "frostbound library" --runner codex --model gpt-5.2
harness animation-theme "quiet moonlit ritual" --runner subprocess
```

The command is intentionally limited to agentic runners because the task requires file edits.

**Arguments:**

| Argument | Type | Description |
|----------|------|-------------|
| `theme` | text | Theme or mood to translate into animation verbs |

**Options:**

| Option | Short | Choices | Description |
|--------|-------|---------|-------------|
| `--runner` | `-r` | `subprocess`, `sdk`, `codex` | Signed-in coding agent to use |
| `--model` | | text | Optional Claude Code/Codex model override |
| `--timeout` | | seconds | Maximum wait for the theme edit |

The invoked agent follows `docs/technical/animation_theme_agent_guide.md`, editing `PHRASES["playful"]` in `harness/ui/spinner.py` with short single verbs only.

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
| `planner_model` | str | `"claude-opus-4-7"` | Model for PlannerAgent in `orchestration_mode='api'` |
| `generator_model` | str | `"claude-opus-4-7"` | Reserved for `orchestration_mode='api'` |
| `evaluator_model` | str | `"claude-opus-4-7"` | Model for EvaluatorAgent in `orchestration_mode='api'` |
| `code_runner_model` | str\|None | `None` | Model passed to Claude Code / Codex as `--model` |
| `openai_api_key` | str\|None | `None` | Documents the OpenAI key the project expects (set `OPENAI_API_KEY` env var to use it) |
| `gemini_api_key` | str\|None | `None` | Documents the Gemini key the project expects (set `GEMINI_API_KEY` env var) |
| `openrouter_api_key` | str\|None | `None` | Documents the OpenRouter key the project expects (set `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`) |
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
orchestrator = Orchestrator(config, runner_type=RunnerType.CODEX)

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
print(result.input_tokens)     # int | None (populated by SDK runner)
print(result.output_tokens)    # int | None
print(result.cost_usd)         # float | None
print(result.tool_calls_observed)  # list[str] (SDK runner only)
print(result.rate_limit_reset_at)  # datetime | None (set if a subscription cap is hit)
```

---

### Using runners directly

```python
from harness.runners import SubprocessRunner, SDKRunner, CodexRunner

# Claude Code CLI
runner = SubprocessRunner(config)
result = runner.implement(prompt, cwd="/my/project")

# Claude Code via OpenRouter (set env vars BEFORE constructing the runner)
import os
os.environ["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api/v1"
os.environ["ANTHROPIC_AUTH_TOKEN"] = os.environ["OPENROUTER_API_KEY"]
config.code_runner_model = "anthropic/claude-sonnet-4-6"
runner = SubprocessRunner(config)
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
# Three coding-agent runners only. Leave null to be prompted.
#   subprocess  — Claude Code CLI
#   sdk         — Claude Code SDK
#   codex       — Codex CLI
# Direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) plug in via
# env vars below; they are not standalone runners.
code_runner: null
code_runner_model: null        # Claude Code/Codex --model, or runner default
codex_oss: false               # use Codex OSS provider routing
codex_local_provider: null     # ollama | lmstudio
code_runner_extra_args: []     # advanced runner CLI flags

# ── API keys (documentation only — set the env vars in your shell) ───────────
# These fields persist what a project expects so it's reproducible. The
# harness does NOT auto-export them to the runner subprocess.
#   ANTHROPIC_API_KEY     → Claude Code / SDK (Claude API mode)
#   OPENAI_API_KEY        → Codex (OpenAI API mode)
#   GEMINI_API_KEY        → routed via Codex custom provider or OpenRouter
#   ANTHROPIC_BASE_URL +
#   ANTHROPIC_AUTH_TOKEN  → Claude Code via OpenRouter (token = OpenRouter key)
openai_api_key: null
gemini_api_key: null
openrouter_api_key: null

# ── Model selection ──────────────────────────────────────────────────────────
# In runner orchestration mode (default), all four agents use the selected
# coding-agent runtime, and code_runner_model controls the model behind it.
# In api orchestration mode (--with-api), planner/evaluator/initializer use
# the Anthropic API directly with the *_model fields below; the generator
# still uses code_runner_model via the agentic runner.
planner_model: "claude-opus-4-7"
generator_model: "claude-opus-4-7"
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

# ── Terminal progress animation ──────────────────────────────────────────────
progress_animation: "sparkle"       # sparkle | bloom | snow | braille | orbit | pulse | dots | moon | bars | clock | wave | tech
progress_phrase_style: "playful"    # playful | steady
progress_text_effect: "typewriter"  # none | typewriter | scramble
```

---

## Environment Variables

| Variable | Mode | Description |
|----------|---------|-------------|
| *(none)* | Claude / Codex subscription | Default — uses your signed-in plan |
| `ANTHROPIC_API_KEY` | Claude API, or `--with-api` orchestration | Pay-per-token Anthropic auth for Claude Code; also required for `orchestration_mode='api'` planner+evaluator |
| `OPENAI_API_KEY` | OpenAI API | Pay-per-token OpenAI auth for Codex |
| `GEMINI_API_KEY` | Gemini API | Routed via Codex custom provider or OpenRouter through Claude Code |
| `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | OpenRouter API | Point Claude Code at OpenRouter's Anthropic-compatible endpoint; `ANTHROPIC_AUTH_TOKEN` should equal your `OPENROUTER_API_KEY` |

The harness does not auto-export these — set them in your shell, in `direnv`, or in your project's `.env` so Claude Code / Codex pick them up.
