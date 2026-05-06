# Architecture Decision Records — Claude Agent Harness

**Last updated:** 2026-05-03  

---

## ADR-001: Context Resets over Compaction
*(unchanged)*

**Decision:** Use context resets with `HandoffDocument` rather than in-place compaction.  
**Rationale:** Resets eliminate context anxiety; compaction retains the model state that caused it.

---

## ADR-002: Separate Generator and Evaluator Agents
*(unchanged)*

**Decision:** Adversarial `EvaluatorAgent` with structured tool-use scoring.  
**Rationale:** Self-evaluation bias is intractable; separation creates a GAN-like quality loop.

---

## ADR-003: One Feature Per Session
*(unchanged)*

**Decision:** Each generator session implements exactly one feature.  
**Rationale:** Prevents context exhaustion and undocumented half-finished work.

---

## ADR-004: File-Based Agent Communication
*(unchanged)*

**Decision:** All shared state in files on disk (`features.json`, handoffs, git).  
**Rationale:** Survives crashes, human-inspectable, no external infrastructure.

---

## ADR-005: Pydantic for All Data Models
*(unchanged)*

**Decision:** Pydantic v2 for `Feature`, `ProjectProgress`, `EvaluationResult`, etc.  
**Rationale:** Built-in JSON round-trip, validation, IDE support.

---

## ADR-006: Sprint Contracts Before Implementation
*(unchanged)*

**Decision:** Generator proposes acceptance criteria before writing code.  
**Rationale:** Aligns generator and evaluator on "done" before work starts.

---

## ADR-007: Pluggable Runner Architecture

**Status:** Accepted  
**Date:** 2026-05-03  

### Context
The initial harness called the Anthropic API directly from `GeneratorAgent`. This created two problems:
1. Users with Claude subscriptions were billed at API rates instead of using their included subscription
2. Users on other providers (OpenAI, Gemini) couldn't use the harness at all

### Decision
Extract all execution logic into a `CodeRunner` abstract class with a single interface (`implement(prompt, cwd) → RunResult`). Implement runners only for **coding agents** (Claude Code CLI, Claude Code SDK, Codex CLI). Direct API providers are not standalone runners — they plug in as the *model* behind one of the agents.

### Rationale
- Single interface keeps the orchestrator completely provider-agnostic
- Coding agents (Claude Code, Codex) provide the harness's actual value: tool use, file I/O, shell, git workflow. A single-turn API call cannot match this.
- Two agents × six auth modes (subscription / Anthropic API / OpenAI API / Gemini / OpenRouter / etc.) cover the realistic deployment matrix without bloating the runner count.
- Runner swap requires only a config change or CLI flag — no code changes

### Consequences
- `GeneratorAgent` constructor now requires a `CodeRunner` instance
- `Orchestrator` resolves runner at startup and injects it
- Sprint contract negotiation remains on the Anthropic API (it's a structured tool call, not an agentic task — no file I/O needed)
- Token/cost data is `None` for agentic transports that don't surface per-call usage (the SDK transport does expose tokens via streaming)

---

## ADR-008: Two Coding Agents, Six Modes (revised 2026-05-06)

**Status:** Accepted (revises 2026-05-03)
**Date:** 2026-05-06

### Context
The earlier design exposed direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) as their own first-class runners. In practice, that gave users an inferior single-turn experience for the same money — no file I/O, no tool use, no agentic behavior. The harness's value comes from running on top of a real coding agent.

### Decision
The harness recognizes exactly **two coding agents** and **six modes**:

| # | Mode | Agent | Auth source |
|---|---|---|---|
| 1 | Claude subscription | Claude Code (subprocess / sdk) | Pro/Max plan |
| 2 | Claude API | Claude Code | `ANTHROPIC_API_KEY` |
| 3 | Codex subscription | Codex | OpenAI Plus plan |
| 4 | OpenAI API | Codex | `OPENAI_API_KEY` |
| 5 | Gemini API | Codex (custom provider) or Claude Code (via OpenRouter) | `GEMINI_API_KEY` |
| 6 | OpenRouter API | Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY` |

The four standalone API runners are removed. API keys flow into the three coding-agent runners via env vars.

### Rationale
- Running `claude --print` (or the SDK) is the same agent regardless of how the model is paid for — picking a billing source is orthogonal to picking the agent
- Removing the four runners eliminates ~600 lines of pay-per-token API code that duplicated what Claude Code / Codex already do better
- Users keep every previously-supported provider; they just access it through a tool-using agent shell instead of as a single-turn text generator

### Consequences
- `harness runners` and the interactive picker show only the three coding-agent options
- The four named CLI flags (`--anthropic-api`, `--openai-api`, `--gemini`, `--openrouter`) are removed; users select the agent (`--claude-code` / `--claude-sdk` / `--codex`) and set the matching env var
- `RunnerType.api_based()` returns `[]` (kept as a method so callers don't break)
- Existing project configs that reference removed runner values must migrate — `harness import` / `harness resume` surface a clear error

---

## ADR-009: Runner Selection via Three-Tier Priority

**Status:** Accepted  
**Date:** 2026-05-03  

### Context
Runner selection needs to be flexible for both first-time users (want guidance) and power users (want to skip prompts).

### Decision
Resolve runner with this priority:
1. `--runner` / `-r` CLI flag — highest; useful for scripting and one-off overrides
2. `code_runner:` in YAML config — for projects that always use the same runner
3. Interactive prompt — shown only when neither of the above is set

### Rationale
- CLI flag enables scripting (`harness run config.yaml -r subprocess`) without touching config
- Config field enables "set it and forget it" per-project defaults
- Interactive prompt with a formatted table ensures new users understand their options
- No hidden defaults — if the user hasn't chosen, they're asked

### Consequences
- `Orchestrator.__init__` accepts an optional `runner_type` argument
- CLI `run` command has a `--runner` option
- `HarnessConfig` has an optional `code_runner` string field

---

## ADR-010: Orchestration Mode Controls Agent Routing

**Status:** Superseded by runner/api orchestration modes  
**Date:** 2026-05-03  

### Context
Should all agents (Planner, Evaluator, Initializer) be routed through the chosen runner?

### Decision
`GeneratorAgent.implement_feature()` always uses the selected `CodeRunner`. `InitializerAgent`, `PlannerAgent`, and `EvaluatorAgent` follow `orchestration_mode`:

- `runner`: use the selected coding-agent runtime too
- `api`: call the Anthropic API directly

### Rationale
- Runner mode lets Claude Code/Codex act as the full coding-agent frame without requiring API keys
- API mode remains useful when precise Anthropic API control is preferred for planning/evaluation
- The evaluator has runner-mode XML parsing as a fallback when API tool use is not available

### Consequences
- Subscription-first runs can be fully API-key-free
- API orchestration still requires `ANTHROPIC_API_KEY`
- Model selection for agentic runtimes lives in `code_runner_model`
