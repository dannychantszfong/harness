# User Stories — Claude Agent Harness

**Version:** 2.0  
**Status:** Active  
**Last updated:** 2026-05-03  

---

## Epic 1: Project Setup
*(unchanged — US-01 through US-03)*

**US-01** — As a developer, I want to describe my project in 1–4 sentences and have the harness automatically decompose it into an ordered feature list.

**US-02** — As a developer, I want an `init.sh` script generated for my project so any session can verify the app starts in one command.

**US-03** — As a developer, I want an initial git commit created automatically so I have a clean baseline to compare against.

---

## Epic 2: Planning
*(unchanged — US-04)*

**US-04** — As a developer, I want my brief expanded into a full product spec with scope boundaries and success criteria.

---

## Epic 3: Runner Selection *(new in v2.0)*

**US-14** — As a Claude subscriber, I want to use my existing subscription to run the harness, so I'm not charged extra API fees on top of my plan.

> Acceptance criteria:
> - `harness run harness_config.json --runner subprocess` uses `claude --print` and my subscription
> - `harness run harness_config.json --runner sdk` uses `claude_code_sdk` and my subscription
> - No `ANTHROPIC_API_KEY` is required for agentic runners
> - Console confirms "using subscription" at startup

---

**US-15** — As a developer with an Anthropic API key but no Claude subscription, I want to use Mode 2 (Claude API) so Claude Code authenticates against my API key instead.

> Acceptance criteria:
> - With `ANTHROPIC_API_KEY` set, `harness run harness_config.json --runner subprocess` uses the API key for Claude Code authentication
> - No subscription login is required for this mode
> - Claude Code's pay-per-token billing applies — no separate "anthropic" runner needed

---

**US-16** — As a developer who wants model flexibility, I want to route Claude Code through OpenRouter so I can use any model OpenRouter exposes.

> Acceptance criteria:
> - With `ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1` and `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY`, `harness run harness_config.json --runner subprocess` routes through OpenRouter
> - `code_runner_model: anthropic/claude-sonnet-4-6` (or any OpenRouter model ID) selects the model
> - Any valid OpenRouter model ID is accepted without code changes
> - The harness does not auto-export these env vars — the user sets them in their shell, `direnv`, or `.env`

---

**US-17** — As a new user, I want an interactive runner selection menu at startup, so I understand my options before committing.

> Acceptance criteria:
> - If `code_runner` is not set in config and `--runner` is not passed, a formatted table is shown
> - Table shows the three coding-agent runners (`subprocess`, `sdk`, `codex`) with billing and requirements
> - Default selection is `subprocess`
> - `harness runners` shows the same table without starting a run

---

**US-18** — As an AI engineer comparing models, I want to switch the model behind the same runner so I can benchmark outputs.

> Acceptance criteria:
> - Switching `code_runner_model` in config (or via `--model`) changes the model used by the agentic runner without changing the runner itself
> - Same `features.json` can be used across model switches (progress is preserved)
> - Selected model is shown in the runner status banner

---

**US-19** — As an OpenAI Codex user, I want to use the `codex` CLI as the runner, so my OpenAI subscription covers the implementation.

> Acceptance criteria:
> - `harness run harness_config.json --runner codex` invokes the `codex` binary
> - If `codex` is not on PATH, the error message includes installation instructions
> - Codex runner runs non-interactively (`codex exec --dangerously-bypass-approvals-and-sandbox`)

---

## Epic 4: Implementation Loop
*(unchanged — US-05 through US-08)*

**US-05** — Sprint contract negotiation before implementation.

**US-06** — Generator implements exactly one feature at a time.

**US-07** — Evaluator feedback fed back to generator automatically.

**US-08** — Every passing feature committed to git immediately.

---

## Epic 5: Context Management
*(unchanged — US-09 through US-10)*

**US-09** — Context reset with HandoffDocument when token budget exceeded.

**US-10** — Standardized session startup checklist.

---

## Epic 6: Observability
*(unchanged — US-11 through US-12)*

**US-11** — Human-readable `progress.md` updated after every feature.

**US-12** — `harness status` command for current progress.

---

## Epic 7: Configuration
*(updated)*

**US-13** — As an AI engineer, I want to tune evaluation weights per project type.

**US-20** — As a developer, I want to set my runner in `harness_config.json` so I never have to answer the interactive prompt on repeated runs.

> Acceptance criteria:
> - `"code_runner": "subprocess"` in `harness_config.json` skips the prompt
> - Priority order: CLI flag > config file > interactive prompt
> - Changing `code_runner` in config takes effect on next `harness run`

**US-21** — As a developer, I want to document the provider keys a project expects in `harness_config.json` so the next person can reproduce the setup.

> Acceptance criteria:
> - `openai_api_key`, `gemini_api_key`, `openrouter_api_key` fields exist in `harness_config.json` and persist across `harness new` / `harness resume`
> - These fields are documentation only — the harness does NOT auto-export them; the user must set the matching env var (`OPENAI_API_KEY`, etc.) in their shell so Claude Code or Codex picks it up
> - For secrets in production, env vars should be set via `direnv`, `.env`, or a secrets manager rather than committed to project config
