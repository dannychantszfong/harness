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
- Support both Claude Code and Codex as the underlying coding agent

### Should Have (P1)
- Interactive runner selection at startup with clear billing/capability comparison
- Runner specified in YAML config or CLI flag (skip interactive prompt)
- Sprint contract negotiation before each feature implementation
- Git commit per passing feature as a recovery point
- Six supported billing/auth modes: Claude subscription, Claude API, Codex subscription, OpenAI API, Gemini API, OpenRouter API — all routed through the two coding agents

### Nice to Have (P2)
- Cost tracking per session (token counts + USD estimate where available)
- Playwright MCP integration for browser-based verification in the evaluator
- `harness runners` command to list all options

---

## 3. Users

| User | Description | Primary Mode |
|------|-------------|-------------|
| Developer (Claude subscriber) | Pro/Max plan, wants to use subscription | Mode 1 — Claude Code via subscription |
| Developer (OpenAI subscriber) | Codex access, prefers GPT models | Mode 3 — Codex via subscription |
| AI Engineer | Testing harness architecture across providers | Switches modes per project |
| Cost-conscious user | Wants cheapest capable model | Mode 5 (Gemini) or Mode 6 (OpenRouter) |
| Enterprise team | Needs specific approved provider | Mode 6 — OpenRouter routes to any model |

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

**Two coding agents, three runner transports:**

| Agent | Runner(s) | Description |
|--------|-----------|-------------|
| Claude Code | `subprocess`, `sdk` | Full tool-using agent. Two transports onto the same agent. |
| Codex | `codex` | Full tool-using agent (OpenAI's). |

API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not standalone runners — they plug into one of the two agents above as the underlying *model* via env vars.

**Acceptance criteria:**
- All 3 runners implement the same `CodeRunner` interface (`implement(prompt, cwd) → RunResult`)
- Switching runners requires only a config change or CLI flag — no code changes
- Each runner validates its prerequisites (binary on PATH, env vars present for the chosen mode) and returns a clear error if missing
- `harness runners` lists the three coding-agent options with billing and requirements

---

### F-04: Runner Selection (Three-Tier Priority)

**Acceptance criteria:**
- `--runner subprocess` CLI flag overrides everything
- `code_runner: subprocess` in YAML config skips the interactive prompt
- If neither is set, an interactive prompt with a formatted table is shown at startup
- Selected runner is printed to console before the harness begins

---

### F-05: Coding-Agent Runners

Three runners across two coding agents:

| Runner | Binary / Library | Agent |
|--------|------------------|-------|
| `subprocess` | `claude --print --dangerously-skip-permissions` | Claude Code |
| `sdk` | `claude_code_sdk.query()` | Claude Code |
| `codex` | `codex exec --dangerously-bypass-approvals-and-sandbox` | Codex |

**Acceptance criteria:**
- All three runners write files to `output_dir`, run bash commands, and commit git
- If the required binary or package is missing, the runner returns a `RunResult(success=False, error=...)` with installation instructions
- The `sdk` runner exposes per-call token usage; `subprocess` and `codex` return `None` for token fields (subscription pricing is opaque)
- `code_runner_model` in config is passed to the agent CLI as `--model` (or `ClaudeCodeOptions(model=...)` for SDK)

---

### F-06: Six Modes (Agent + Auth Source)

The harness supports six combinations of coding agent and billing/auth source. Modes are not separate runners — they are env-var configurations on top of the three runners above.

| # | Mode | Agent / Runner | Auth source |
|---|------|----------------|-------------|
| 1 | Claude subscription | Claude Code (`subprocess` or `sdk`) | Pro / Max plan |
| 2 | Claude API | Claude Code | `ANTHROPIC_API_KEY` |
| 3 | Codex subscription | Codex | OpenAI Plus plan |
| 4 | OpenAI API | Codex | `OPENAI_API_KEY` |
| 5 | Gemini API | Codex (custom provider) or Claude Code (via OpenRouter) | `GEMINI_API_KEY` |
| 6 | OpenRouter API | Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY` |

**Acceptance criteria:**
- Switching modes requires no code changes — just env var changes
- The harness does not auto-export env vars from `config.yaml`; users set them in their shell, `direnv`, or `.env`
- A subscription rate-limit hit (Modes 1, 3) is detected, surfaced with a friendly panel, and (by default) auto-resumed via launchd at the reset time

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
