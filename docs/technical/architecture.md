# Architecture Document — Claude Agent Harness

**Version:** 2.0  
**Status:** Active  
**Last updated:** 2026-05-03  

---

## 1. System Overview

The Claude Agent Harness is a Python orchestration framework that drives multiple specialised AI agents through a defined lifecycle to build software projects. Version 2.0 introduces a **pluggable runner layer** that decouples the orchestration logic from the model provider, enabling seamless switching between Claude Code, OpenAI Codex, and any pay-per-token API.

---

## 2. Layer Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI Layer          cli.py                                       │
│  harness run / status / init / plan / runners                    │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│  Orchestration Layer   harness/orchestrator.py                   │
│  Phase sequencing · GAN loop · context reset · token tracking    │
└──────┬──────────────┬──────────────┬──────────────┬────────────┘
       │              │              │              │
┌──────▼──────┐ ┌─────▼──────┐ ┌───▼────────┐ ┌──▼──────────────┐
│  Initializer│ │  Planner   │ │  Generator │ │   Evaluator     │
│  (one-time) │ │  (spec)    │ │  + Runner ◄├─┤ (GAN grader)    │
└──────┬──────┘ └─────┬──────┘ └───┬────────┘ └──────┬──────────┘
       │              │             │                  │
┌──────▼──────────────▼─────────────▼──────────────────▼─────────┐
│  Runner Layer   harness/runners/  (3 coding-agent transports)    │
│                                                                  │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                  │
│  │ subprocess │  │    sdk     │  │   codex    │                  │
│  │ Claude CLI │  │ Claude SDK │  │ Codex CLI  │                  │
│  └────────────┘  └────────────┘  └────────────┘                  │
│                                                                  │
│  API providers (Anthropic / OpenAI / Gemini / OpenRouter) plug    │
│  into one of the three above via env vars; no separate runners.  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  Persistence Layer   Disk                                        │
│  features.json · progress.md · handoff_*.json · init.sh · git   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 Orchestrator (`harness/orchestrator.py`)

Unchanged from v1.0 except:
- Accepts a `RunnerType` at construction time
- Passes the resolved `CodeRunner` instance to `GeneratorAgent`
- Logs the selected runner at startup

**Runner resolution order:**
1. `runner_type` argument to `Orchestrator(config, runner_type=...)`
2. `config.code_runner` string from JSON
3. Default: `RunnerType.SUBPROCESS`

---

### 3.2 Runner Layer (`harness/runners/`) ← rewritten in v2.1

The runner is the only component that talks to an external execution environment. Everything else in the harness is provider-agnostic.

The harness supports **two coding agents** behind **six billing/auth modes**. Direct API providers are not standalone runners — they plug into Claude Code or Codex as the underlying model.

#### `CodeRunner` (abstract base)

```python
class CodeRunner(ABC):
    def implement(self, prompt: str, cwd: str, timeout_seconds: int) -> RunResult:
        ...
```

All runners implement this single method. `RunResult` carries:
- `output: str` — the agent's final self-evaluation text
- `success: bool`
- `error: str | None`
- `rate_limit_reset_at: datetime | None` — set when a subscription cap is hit
- `input_tokens / output_tokens / cost_usd` — populated when the runner exposes them (SDK), `None` otherwise

#### `RunnerType` enum

```
SUBPROCESS  — subprocess_runner.py  — claude --print           (Claude Code CLI)
SDK         — sdk_runner.py         — claude_code_sdk.query()  (Claude Code SDK)
CODEX       — codex_runner.py       — codex exec               (Codex CLI)
```

#### Six modes mapped to two agents

| # | Mode | Agent | Auth source |
|---|---|---|---|
| 1 | Claude subscription | Claude Code (subprocess / sdk) | Pro/Max plan |
| 2 | Claude API | Claude Code | `ANTHROPIC_API_KEY` |
| 3 | Codex subscription | Codex | OpenAI Plus plan |
| 4 | OpenAI API | Codex | `OPENAI_API_KEY` |
| 5 | Gemini API | Codex (custom provider) or Claude Code (via OpenRouter) | `GEMINI_API_KEY` |
| 6 | OpenRouter API | Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY` |

All six modes go through the same `CodeRunner` interface, so the orchestrator never branches on billing model.

---

### 3.3 Agent Layer (`harness/agents/`)

**`GeneratorAgent`**:
- Constructor accepts `runner: CodeRunner`
- `implement_feature()` calls `self.runner.implement(prompt, cwd)` instead of `self._call()`
- Token usage from `RunResult` is accumulated into `self.usage`

**Orchestration agents** (`InitializerAgent`, `PlannerAgent`, `EvaluatorAgent`) route according to `orchestration_mode`:
- `runner` mode: use the same coding-agent runtime as the generator, so Claude Code/Codex can carry the full workflow without API keys
- `api` mode: use the Anthropic API directly for planner/evaluator structure while the generator uses the selected runner

The model inside an agentic runner is selected through `code_runner_model` and passed to Claude Code/Codex as `--model`.

---

### 3.4 Context Management, Progress Tracking, Session Opener

Unchanged from v1.0. See [architecture v1.0 sections 2.3–2.5].

---

## 4. Data Model

Unchanged from v1.0. `RunResult` is a transient dataclass (not persisted).

---

## 5. Communication Patterns

### New in v2.0: Runner → Disk (agentic runners)
Agentic runners write files directly to `output_dir` via the underlying tool (Claude Code / Codex). The harness does not intermediate these writes — they happen inside the runner's subprocess/SDK call. The harness only sees the final `RunResult.output` string.

### All other patterns unchanged
See architecture v1.0 for agent→agent, agent→disk, agent→API patterns.

---

## 6. Key Design Decisions

See [ADR.md](ADR.md). Summary of additions in v2.0:

| Decision | Choice | Rejected Alternative |
|----------|--------|---------------------|
| Runner interface | Single `implement(prompt, cwd) → RunResult` | Separate interfaces per family |
| Agent routing | `orchestration_mode` controls runner vs API routing | Hard-code all agents to one provider |
| Coding-agent model | `code_runner_model` passed to Claude Code/Codex | Treat model choice as API-only |
| Provider precedence | CLI flag > config > prompt | Config-only, no CLI flag |

---

## 7. Environment Setup

```bash
# Required: Python 3.11+
conda create -n harness python=3.12 -y
conda activate harness

# Core install
pip install -e .

# Optional extras
pip install -e ".[sdk]"           # Claude Code SDK transport
```

External binaries (install separately, not via pip):

```bash
# Claude Code CLI — required for the subprocess runner
#   https://claude.ai/download

# Codex CLI — required for the codex runner
#   https://github.com/openai/codex
```
