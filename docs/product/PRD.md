# Product Requirements Document — Claude Agent Harness

**Version:** 2.0  
**Status:** Active  
**Owner:** Engineering  
**Last updated:** 2026-05-03  

---

## 1. Overview

The Claude Agent Harness is a production orchestration framework for long-running AI coding agents. It solves three fundamental problems in agentic AI systems: context exhaustion, self-evaluation bias, and provider lock-in.

### Problem Statement

| Problem | Symptom | Root Cause |
|---------|---------|-----------|
| Context exhaustion | Agent wraps up work prematurely | "Context anxiety" near token limits |
| Self-evaluation bias | Agent rates its own output highly regardless of quality | Generator and evaluator are the same model |
| Broken multi-session continuity | New session has no memory of prior work | No structured knowledge transfer |
| Premature completion | Agent declares "done" before all features exist | No objective completion criteria |
| Provider lock-in | Switching from Claude to OpenAI requires rewriting the harness | Runner is tightly coupled to orchestration |

---

## 2. Goals

### Must Have (P0)
- Drive an AI agent to build a full software project from a one-paragraph brief
- Support arbitrary-length projects spanning multiple sessions and context resets
- Produce scored, evaluated output (GAN-style generator/evaluator loop)
- Track feature-level progress with pass/fail state persisted across sessions
- Support at minimum two runner backends (Claude Code CLI and direct API)

### Should Have (P1)
- Interactive runner selection at startup with clear billing/capability comparison
- Runner specified in YAML config or CLI flag (skip interactive prompt)
- Sprint contract negotiation before each feature implementation
- Git commit per passing feature as a recovery point
- Support OpenAI, Gemini, and OpenRouter as API runner alternatives

### Nice to Have (P2)
- Cost tracking per session (token counts + USD estimate where available)
- Playwright MCP integration for browser-based verification in the evaluator
- `harness runners` command to list all options

---

## 3. Users

| User | Description | Primary Use |
|------|-------------|-------------|
| Developer (Claude subscriber) | Pro/Max plan, wants to use subscription | `subprocess` or `sdk` runner |
| Developer (OpenAI subscriber) | Codex access, prefers GPT models | `codex` or `openai` runner |
| AI Engineer | Testing harness architecture across providers | Compares runner outputs |
| Cost-conscious user | Wants cheapest capable model | `gemini` or `openrouter` runner |
| Enterprise team | Needs specific approved provider | `openrouter` (routes to any model) |

---

## 4. Features

### F-01: Project Initialization
*(unchanged from v1.0)*

---

### F-02: Brief → Spec Expansion (Planner)
*(unchanged from v1.0)*

---

### F-03: Pluggable Runner System

The **runner** is the component that executes agentic implementation work. The orchestration logic (GAN loop, context resets, progress tracking) is completely decoupled from the runner.

**Runner families:**

| Family | Description | File I/O |
|--------|-------------|---------|
| Agentic | Drives a full tool-using agent (Claude Code, Codex) | ✅ Writes files directly |
| API | Single-turn model call, text output | ❌ Text description only |

**Acceptance criteria:**
- All 7 runners implement the same `CodeRunner` interface (`implement(prompt, cwd) → RunResult`)
- Switching runners requires only a config change or CLI flag — no code changes
- Each runner validates its prerequisites (binary on PATH, API key set) and returns a clear error if missing
- `harness runners` lists all options with billing and requirements

---

### F-04: Runner Selection (Three-Tier Priority)

**Acceptance criteria:**
- `--runner subprocess` CLI flag overrides everything
- `code_runner: subprocess` in YAML config skips the interactive prompt
- If neither is set, an interactive prompt with a formatted table is shown at startup
- Selected runner is printed to console before the harness begins

---

### F-05: Agentic Runners (Subscription-Based)

Three agentic runners that drive full tool-using agents:

| Runner | Binary | Subscription |
|--------|--------|-------------|
| `subprocess` | `claude --print --dangerously-skip-permissions` | Claude Pro/Max |
| `sdk` | `claude_code_sdk.query()` | Claude Pro/Max |
| `codex` | `codex --approval-mode full-auto` | OpenAI |

**Acceptance criteria:**
- Agentic runners write files to `output_dir`, run bash commands, and commit git
- If the required binary is not on PATH, the runner returns a `RunResult(success=False, error=...)` with installation instructions
- Token/cost data is `None` for agentic runners (subscription pricing, not per-token)

---

### F-06: API Runners (Pay-Per-Token)

Four API runners for pay-per-token usage:

| Runner | Provider | Default Model | Key env var |
|--------|----------|---------------|-------------|
| `anthropic` | Anthropic | `claude-opus-4-7` | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI | `gpt-4o` | `OPENAI_API_KEY` |
| `gemini` | Google | `gemini-2.5-pro` | `GEMINI_API_KEY` |
| `openrouter` | OpenRouter | `anthropic/claude-opus-4-7` | `OPENROUTER_API_KEY` |

**Acceptance criteria:**
- API runners stream output in real time
- Token counts and estimated cost (USD) are returned in `RunResult`
- Missing API key returns `RunResult(success=False, error=...)` with instructions
- Missing provider package returns `RunResult(success=False, error="pip install ...")` 
- `generator_model` in config controls which model each API runner uses
- OpenRouter runner accepts any model ID from openrouter.ai

---

### F-07: GAN Feedback Loop
*(unchanged from v1.0)*

---

### F-08: Context Reset with Structured Handoff
*(unchanged from v1.0)*

---

### F-09: Session Opening Checklist
*(unchanged from v1.0)*

---

### F-10: CLI Interface

| Command | Description |
|---------|-------------|
| `harness run config.yaml` | Full end-to-end run (prompts for runner if not configured) |
| `harness run config.yaml --runner subprocess` | Run with specific runner (no prompt) |
| `harness runners` | List all runners with billing/requirements table |
| `harness status config.yaml` | Print progress (no API calls) |
| `harness init config.yaml "brief"` | Initialize only |
| `harness plan config.yaml` | Run planner only |

---

## 5. Non-Goals

- Parallelising feature implementation across multiple agents
- Hosted/multi-tenant service
- Real-time collaborative editing
- Automatic model selection based on cost/performance benchmarks

---

## 6. Success Metrics

| Metric | Target |
|--------|--------|
| Feature passing rate | ≥ 80% of features pass within max iterations |
| Evaluation score on passing features | ≥ 8.0 / 10 |
| Context reset recovery rate | 100% (no features lost across resets) |
| Runner swap — no code change required | ✅ Config/flag only |
| Runner error messages actionable | User can resolve without reading source code |
