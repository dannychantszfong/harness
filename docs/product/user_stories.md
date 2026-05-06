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
> - `harness run config.yaml --runner subprocess` uses `claude --print` and my subscription
> - `harness run config.yaml --runner sdk` uses `claude_code_sdk` and my subscription
> - No `ANTHROPIC_API_KEY` is required for agentic runners
> - Console confirms "using subscription" at startup

---

**US-15** — As a developer without Claude Code installed, I want to use the OpenAI or Gemini API instead, so I can still run the harness.

> Acceptance criteria:
> - `harness run config.yaml --runner openai` uses my `OPENAI_API_KEY`
> - `harness run config.yaml --runner gemini` uses my `GEMINI_API_KEY`
> - A clear error is shown if the API key is missing
> - A clear error is shown if the required package (`openai`, `google-generativeai`) is not installed

---

**US-16** — As a developer who wants model flexibility, I want to route through OpenRouter so I can use any available model.

> Acceptance criteria:
> - `harness run config.yaml --runner openrouter` routes to OpenRouter
> - `generator_model: anthropic/claude-opus-4-7` in config selects the model
> - Any valid OpenRouter model ID is accepted without code changes
> - `OPENROUTER_API_KEY` environment variable or `openrouter_api_key` config field is used

---

**US-17** — As a new user, I want an interactive runner selection menu at startup, so I understand my options before committing.

> Acceptance criteria:
> - If `code_runner` is not set in config and `--runner` is not passed, a formatted table is shown
> - Table shows: runner name, family, billing model, file I/O capability, what's required
> - Default selection is `subprocess`
> - `harness runners` shows the same table without starting a run

---

**US-18** — As an AI engineer comparing providers, I want to switch runners with a single flag, so I can benchmark outputs across models.

> Acceptance criteria:
> - `harness run config.yaml --runner anthropic` and `harness run config.yaml --runner openai` produce comparable outputs for the same feature
> - Same `features.json` can be used across runner switches (progress is preserved)
> - Runner name is logged in the session output for attribution

---

**US-19** — As an OpenAI Codex user, I want to use the `codex` CLI as the agentic runner, so my OpenAI subscription covers the implementation.

> Acceptance criteria:
> - `harness run config.yaml --runner codex` invokes the `codex` binary
> - If `codex` is not on PATH, error message includes installation instructions
> - Codex runner runs non-interactively (`--approval-mode full-auto`)

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

**US-20** — As a developer, I want to set my runner in `config.yaml` so I never have to answer the interactive prompt on repeated runs.

> Acceptance criteria:
> - `code_runner: subprocess` in YAML config skips the prompt
> - Priority order: CLI flag > config file > interactive prompt
> - Changing `code_runner` in config takes effect on next `harness run`

**US-21** — As a developer, I want to set provider API keys in config as a fallback to environment variables.

> Acceptance criteria:
> - `openai_api_key: sk-...` in config is used if `OPENAI_API_KEY` env var is not set
> - Env var always takes precedence over config file value
> - Warning is shown if key appears in config (encourage using env vars for secrets)
