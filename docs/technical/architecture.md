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
│  Runner Layer   harness/runners/                                 │
│                                                                  │
│  Agentic (subscription)    │  API (pay-per-token)                │
│  ┌────────────┐            │  ┌────────────┐ ┌────────────┐     │
│  │ subprocess │            │  │ anthropic  │ │  openai    │     │
│  │ sdk        │            │  │ gemini     │ │ openrouter │     │
│  │ codex      │            │  └────────────┘ └────────────┘     │
│  └────────────┘            │                                     │
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
2. `config.code_runner` string from YAML
3. Default: `RunnerType.SUBPROCESS`

---

### 3.2 Runner Layer (`harness/runners/`) ← new in v2.0

The runner is the only component that talks to an external execution environment. Everything else in the harness is provider-agnostic.

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
- `input_tokens / output_tokens / cost_usd` — populated by API runners, `None` for agentic

#### `RunnerType` enum

```
SUBPROCESS  — subprocess_runner.py  — claude --print
SDK         — sdk_runner.py         — claude_code_sdk.query()
CODEX       — codex_runner.py       — codex --approval-mode full-auto
ANTHROPIC   — api_runner.py         — Anthropic messages API
OPENAI      — openai_api_runner.py  — OpenAI chat completions
GEMINI      — gemini_api_runner.py  — Google GenerativeAI
OPENROUTER  — openrouter_api_runner.py — OpenAI-compatible proxy
```

#### Agentic vs API runners

| Dimension | Agentic | API |
|-----------|---------|-----|
| File I/O | Direct (writes to disk) | None (text output only) |
| Billing | Subscription included | Per token |
| Token data | Not available | Returned in RunResult |
| Real-world use | Production builds | Testing / prototyping |
| Context | Full tool use loop | Single-turn call |

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

# Optional provider extras
pip install -e ".[sdk]"           # Claude Code SDK
pip install -e ".[openai]"        # OpenAI + OpenRouter
pip install -e ".[gemini]"        # Google Gemini
pip install -e ".[all-providers]" # Everything
```
