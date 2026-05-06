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
Extract all execution logic into a `CodeRunner` abstract class with a single interface (`implement(prompt, cwd) → RunResult`). Implement 7 concrete runners across two families (agentic, API).

### Rationale
- Single interface keeps the orchestrator completely provider-agnostic
- Agentic runners (subprocess/sdk/codex) use subscriptions — zero extra cost for existing subscribers
- API runners (anthropic/openai/gemini/openrouter) provide pay-per-token alternatives with cost tracking
- Runner swap requires only a config change or CLI flag — no code changes

### Consequences
- `GeneratorAgent` constructor now requires a `CodeRunner` instance
- `Orchestrator` resolves runner at startup and injects it
- Sprint contract negotiation remains on the Anthropic API (it's a structured tool call, not an agentic task — no file I/O needed)
- Token/cost data is `None` for agentic runners (subscription pricing is opaque)

---

## ADR-008: Agentic Runners Use Subscription; API Runners Pay Per Token

**Status:** Accepted  
**Date:** 2026-05-03  

### Context
Users asked whether they could use their Claude subscription instead of paying API fees. The answer depends on *how* the harness calls the model.

### Decision
Split runners into two families:
- **Agentic** (subprocess, sdk, codex): invoke the CLI or SDK, which authenticates via the user's subscription login. No `API_KEY` required.
- **API** (anthropic, openai, gemini, openrouter): call the provider's HTTP API directly. Require an `API_KEY`. Billed per token.

### Rationale
- Running `claude --print` is identical to typing in the terminal — uses subscription
- Running `anthropic.Anthropic().messages.create()` hits the API — billed per token
- These are fundamentally different billing surfaces that the user must opt into knowingly

### Consequences
- `harness runners` and the interactive prompt explicitly show "Claude subscription" vs "pay-per-token"
- Token/cost data is unavailable for agentic runners (no per-token visibility in subscription billing)
- Agentic runners have full file I/O; API runners produce text only

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
