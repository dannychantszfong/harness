# Agent Harness

A production orchestration framework for long-running AI coding agents, implementing the design patterns from Anthropic's engineering articles:

- [Harness Design for Long-Running Application Development](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

Built around **2 coding agents** (Claude Code and Codex) with **6 supported billing/auth modes** — Claude subscription, Claude API, Codex subscription, OpenAI API, Gemini API, OpenRouter API. Direct API providers are not standalone runners; they plug into Claude Code or Codex as the underlying model.

**Core idea:** the harness is built around coding-agent runtimes, not raw model calls. Claude Code and Codex provide the agent architecture — tools, file edits, shell execution, git workflow, and session behavior. The selected model is the engine inside that frame, and users can choose that engine before a project starts.

---

## Quick Start

```bash
# 1. Create conda environment
conda create -n harness python=3.12 -y && conda activate harness
pip install -e ".[all-providers]"

# 2. Start a new project — interactive, no config editing needed
harness new --claude-code        # Claude Code (subscription by default)
harness new --claude-sdk         # Claude Code via SDK (structured output)
harness new --claude-code --model sonnet
harness new --codex --model gpt-5.2
harness new                      # shows interactive runner menu
harness new --claude-code --github-repo owner/my-app

# 3. Resume / import existing work
harness resume output/my_app_a3f8c21b
harness import ../my-other-repo  # detect stage, run from the right phase
harness import ../my-other-repo --github-repo owner/my-other-repo

# 4. Other commands
harness runners                  # list runners with requirements
harness animation-theme "moonlit ritual" --runner codex
harness status output/my_app_a3f8c21b/harness_config.json
```

---

## Architecture

```
harness new
  │
  ├─ Prompts: project name, brief
  ├─ Requirement Alignment (planner ↔ user, back-and-forth until confirmed)
  ├─ Runner selection (flag / menu)
  └─ Saves config to ./output/<slug>_<id>/harness_config.json
        │
        ▼
Phase 1 — Initializer
  └─ Decomposes brief into feature list, writes init.sh, git init

Phase 2 — Planner
  └─ Expands brief into full product spec (skipped if already confirmed)

Phase 3 — Feature Loop (GAN-style)
  ┌──────────────────────────────────────────────────────────┐
  │  Sprint Contract: generator proposes acceptance criteria  │
  │        ↓                                                  │
  │  Generator implements feature via Runner                  │
  │        ↓ self-eval text                                   │
  │  Evaluator grades (design / originality / craft / fn)    │
  │        ↓                                                  │
  │  score ≥ threshold → git commit → next feature            │
  │  score < threshold → feedback injected → iterate (≤ N)   │
  └──────────────────────────────────────────────────────────┘
        │
  Context Reset (if token budget exceeded)
  └─ HandoffDocument → fresh session with preamble
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Context resets over compaction** | Eliminates "context anxiety" — models wrap up prematurely near token limits |
| **GAN-style generator + evaluator** | Self-evaluation bias is intractable; adversarial separation drives quality |
| **One feature per session** | Prevents context exhaustion and undocumented half-finished work |
| **File-based state** | `features.json` is the source of truth — restartable, human-inspectable |
| **Sprint contracts** | Generator and evaluator agree on "done" criteria before implementation |
| **Two orchestration modes** | Pure subscription (no API key) or API orchestration — user's choice |
| **Two coding agents, six billing modes** | All execution flows through Claude Code or Codex; API providers plug in as the model |
| **Independent output repos** | The Harness repo ignores `output/`; each generated/imported project owns its own git repo and optional GitHub remote |

---

## Coding agents and modes

The harness has **two coding agents** that drive all implementation work, and **six supported modes** for paying/authenticating the model behind them. Direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not standalone runners — they plug into one of the two agents via env vars.

### The two agents

| Agent | Internal runners | Capabilities |
|---|---|---|
| **Claude Code** | `subprocess` (CLI), `sdk` (Python SDK) | Full file I/O, shell, git, multi-turn tool use |
| **Codex** | `codex` (CLI) | Full file I/O, shell, git, multi-turn tool use |

`subprocess` and `sdk` are two transports onto the same Claude Code agent — pick `sdk` when you want streamed tool-call telemetry; pick `subprocess` for the lighter setup.

### The six modes

| # | Mode | Agent used | Auth source | Notes |
|---|---|---|---|---|
| 1 | **Claude subscription** | Claude Code | Pro / Max plan | Default for `--claude-code` and `--claude-sdk`. No env var needed. |
| 2 | **Claude API** | Claude Code | `ANTHROPIC_API_KEY` | Pay-per-token through the same Claude Code agent. |
| 3 | **Codex subscription** | Codex | OpenAI Plus subscription | Default for `--codex`. |
| 4 | **OpenAI API** | Codex | `OPENAI_API_KEY` | Pay-per-token through the Codex agent. |
| 5 | **Gemini API** | Codex (custom provider) or Claude Code (via OpenRouter) | `GEMINI_API_KEY` | Routed through one of the two agents. |
| 6 | **OpenRouter API** | Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY` | OpenRouter's OpenAI-compatible Anthropic endpoint. Use for any model OpenRouter exposes. |

The harness does **not** auto-export these env vars; set them in your shell, in `direnv`, or in a project's `.env` and Claude Code / Codex will pick them up.

### Picking a mode

```bash
# Claude subscription — no env, no key
harness new --claude-code

# Claude API
ANTHROPIC_API_KEY=... harness new --claude-code

# Codex subscription
harness new --codex

# OpenAI API
OPENAI_API_KEY=... harness new --codex

# OpenRouter API (route Claude Code through OpenRouter)
ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1 \
ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY \
harness new --claude-code --model anthropic/claude-sonnet-4-6
```

### Coding Agent Model Selection

For Claude Code and Codex runners, `--model` selects the model used inside the coding agent runtime:

```bash
harness new --claude-code --model sonnet
harness new --claude-sdk --model claude-sonnet-4-6
harness new --codex --model gpt-5.2
```

If you omit `--model`, `harness new` asks once during setup. Press Enter to use the runner's own default. The selected value is saved as `code_runner_model` in `harness_config.json`.

Codex local/open-source routing can be configured in `harness_config.json`:

```json
{
  "code_runner": "codex",
  "code_runner_model": "qwen2.5-coder",
  "codex_oss": true,
  "codex_local_provider": "ollama"
}
```

---

## Orchestration Modes

The **orchestration mode** controls how the **planner** and **evaluator** agents run (the generator always uses the agent you selected):

| Mode | Planner + Evaluator | API key needed? | Set via |
|------|--------------------|-----------------|----|
| `runner` | Same agent as generator | **No** — pure subscription | Default |
| `api` | Anthropic API directly | **Yes** — `ANTHROPIC_API_KEY` | `--with-api` flag |

```bash
# Pure subscription — no API key at all
harness new --claude-code

# Split: generator = Claude Code CLI, planner/evaluator = Anthropic API
harness new --claude-code --with-api
```

The runner status banner at startup always shows which mode is active:

```
╭── ✓ Runner ready  (subprocess) ──────────────────────────────────────╮
│ Claude Code CLI  ·  subscription billing  ·  full file I/O           │
│ Binary: /usr/local/bin/claude  (2.1.98 (Claude Code))                │
│                                                                       │
│ Orchestration: runner mode — planner + evaluator use this runner too │
│                              (no API key needed)                      │
╰───────────────────────────────────────────────────────────────────────╯
```

### Quiet Progress Animation

When the harness is waiting on a silent blocking step, it shows a small terminal pulse instead of looking frozen. The default uses rotating symbols plus short phrase transitions:

```text
✧ S
✦ Scrying
✨ Inscribing
```

Built-in animation packs: `sparkle`, `bloom`, `snow`, `braille`, `orbit`, `pulse`, `dots`, `moon`, `bars`, `clock`, `wave`, `tech`. The playful phrase style uses restrained magical verbs like `Scrying`, `Inscribing`, `Warding`, and `Attuning`.

To change the playful verb theme, ask one of your signed-in coding agents to patch the local Harness checkout:

```bash
harness animation-theme "frostbound library" --runner codex --model gpt-5.2
harness animation-theme "quiet moonlit ritual" --runner subprocess
```

The command launches Claude Code or Codex with a narrow guide for editing `PHRASES["playful"]` in `harness/ui/spinner.py`. It keeps the output as single verbs, so the spinner stays like `Scrying`, not `Scrying with Claude Code`.

The animation runs only in interactive terminals and writes to stderr, so logs and piped output stay clean. Disable it with:

```bash
export HARNESS_NO_SPINNER=1
```

---

## Starting a New Project

`harness new` replaces manual config editing with an interactive flow:

```
$ harness new --claude-code

╭─ Agent Harness ────────╮
│ New Project Setup       │
╰─ Let's build something ─╯

Project name: My Todo App
What would you like to build?: A todo app with auth and due dates

Project ID: a3f8c21b
Output dir: ./output/my_todo_app_a3f8c21b

── Requirement Alignment ──
The planner will draft a spec. Review it and give feedback, or press Enter to confirm.

── PLANNER ──
(draft spec appears here…)

╭── Draft Spec (round 1) ──────────╮
│ ## My Todo App                    │
│ ### In Scope                      │
│ …                                 │
╰───────────────────────────────────╯

Feedback (press Enter to confirm, or describe what to change): add dark mode support

(planner refines based on your feedback…)

Feedback (press Enter to confirm, or describe what to change): ↵
✓ Requirements confirmed.

Config saved to ./output/my_todo_app_a3f8c21b/harness_config.json
```

The confirmed spec is injected directly into the orchestrator, skipping an extra planner API call.

---

## Per-Project GitHub Sync

Harness does not store generated apps inside the Harness git history. `output/` is ignored, and every generated or copied project is treated as its own independent repository.

To create or reuse a GitHub repo for the project before the workflow starts:

```bash
# Create/check owner/my-app with gh, then push after init and each passing feature
harness new --claude-code --github-repo owner/my-app

# Import a local repo into output/, then push that copy to its own GitHub repo
harness import ../my-app --github-repo owner/my-app

# Use an existing remote URL instead of gh repo creation
harness new --codex --git-remote git@github.com:owner/my-app.git

# Store the remote setting but do not auto-push during the run
harness new --claude-code --github-repo owner/my-app --no-git-push
```

`--github-repo` requires the GitHub CLI (`gh`) to be installed and authenticated. It sets `origin` to `https://github.com/owner/repo.git`; use `--git-remote` for SSH or any custom remote URL.

---

## Resuming a Project

```bash
harness run output/my_todo_app_a3f8c21b/harness_config.json

# Override the runner (e.g. switch from SDK to subprocess)
harness run output/my_todo_app_a3f8c21b/harness_config.json --runner subprocess
```

The harness is fully restartable. It picks up from `features.json` — features already passing are skipped.

---

## Project Structure

```
agent-harness/
├── harness/
│   ├── agents/
│   │   ├── base.py              # Streaming, prompt caching, _call_via_runner helper
│   │   ├── initializer.py       # One-time setup: features.json, init.sh, git commit
│   │   ├── planner.py           # Brief → spec; align_requirements() back-and-forth loop
│   │   ├── generator.py         # Feature implementation (delegates to runner)
│   │   └── evaluator.py         # Adversarial grader — tool use (api) or XML parsing (runner)
│   ├── runners/
│   │   ├── base.py              # CodeRunner ABC, RunResult, RunnerType, PreflightResult
│   │   ├── subprocess_runner.py # claude --print       (Claude Code via CLI)
│   │   ├── sdk_runner.py        # claude_code_sdk      (Claude Code via Python SDK)
│   │   └── codex_runner.py      # codex CLI            (Codex)
│   ├── context/
│   │   ├── handoff.py           # HandoffDocument: cross-session state transfer
│   │   └── reset.py             # Token budget tracking + context reset logic
│   ├── progress/
│   │   ├── models.py            # Feature, ProjectProgress, EvaluationResult (Pydantic)
│   │   └── tracker.py           # Read/write features.json + progress.md
│   ├── session/
│   │   └── opener.py            # Session startup context builder
│   ├── config.py                # HarnessConfig: JSON-backed, includes orchestration_mode
│   └── orchestrator.py          # Main loop: phases, GAN loop, context resets, runner status
├── tests/
│   ├── test_runners.py          # Runner factory + the three coding-agent runners
│   ├── test_orchestrator.py     # Phase sequencing, GAN loop, max-iteration guard
│   ├── test_progress.py         # Feature model, tracker CRUD
│   └── test_handoff.py          # HandoffDocument save/load/render
├── docs/
│   ├── diagrams/                # draw.io architecture, sequence, state machine diagrams
│   ├── product/                 # PRD, user stories
│   ├── technical/               # Architecture, API reference, ADRs
│   ├── operational/             # Runbook, deployment guide
│   └── testing/                 # Test plan, test cases
├── cli.py
└── pyproject.toml
```

---

## Configuration Reference

Saved automatically to `output/<slug>_<id>/harness_config.json` by `harness new`. You can also write it manually.

```json
{
  "project_name": "my-app",
  "project_id": "a3f8c21b",
  "brief": "One to four sentences describing what to build.",
  "output_dir": "./output/my_app_a3f8c21b",
  "orchestration_mode": "runner",
  "code_runner": "subprocess",
  "code_runner_model": "sonnet",
  "codex_oss": false,
  "codex_local_provider": null,
  "code_runner_extra_args": [],
  "progress_animation": "sparkle",
  "progress_phrase_style": "playful",
  "progress_text_effect": "typewriter",
  "project_git_push": false,
  "project_git_branch": "main",
  "project_git_remote": null,
  "project_github_repo": null,
  "project_github_private": true,
  "openai_api_key": null,
  "gemini_api_key": null,
  "openrouter_api_key": null,
  "planner_model": "claude-opus-4-7",
  "generator_model": "claude-opus-4-7",
  "evaluator_model": "claude-opus-4-7",
  "max_iterations_per_feature": 15,
  "evaluator_pass_score": 8.0,
  "sprint_contract_enabled": true,
  "evaluator_weights": {
    "design_quality": 0.30,
    "originality": 0.30,
    "craft": 0.25,
    "functionality": 0.15
  },
  "context_reset_threshold_tokens": 150000
}
```

---

## Environment Variables

| Variable | Mode |
|----------|------|
| *(none)* | Claude subscription, Codex subscription |
| `ANTHROPIC_API_KEY` | Claude API (Claude Code uses it instead of subscription auth), or `orchestration_mode: api` for split planner/evaluator |
| `OPENAI_API_KEY` | OpenAI API (Codex uses it instead of subscription auth) |
| `GEMINI_API_KEY` | Gemini API (via OpenRouter routing through Claude Code, or via Codex with a custom provider) |
| `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | OpenRouter API — point Claude Code at OpenRouter's Anthropic-compatible endpoint, with the token set to your OpenRouter key |

With `--claude-code`, `--claude-sdk`, or `--codex` in the default runner mode and a paid subscription for the matching agent, **no env var is needed**.

---

## Runner Pre-flight Checks

Every runner validates itself before the first feature runs. If something is wrong (missing binary, no API key), you get a clear red panel and the harness exits immediately — no wasted planner calls:

```
╭── Cannot start ──────────────────────────────────────────────────────╮
│ Runner error: subprocess                                              │
│                                                                       │
│ `claude` binary not found on PATH.                                    │
│ Install Claude Code: https://claude.ai/download                       │
╰───────────────────────────────────────────────────────────────────────╯
```

---

## Running Tests

```bash
conda activate harness
python -m pytest tests/ -v          # 99+ tests, no API key needed — all mocked
python -m pytest tests/test_runners.py -v   # runner-specific coverage
```

---

## Cost Reference

| Mode | Billing model | Typical cost per feature (5 iterations) |
|--------|--------------|----------------------------------------|
| Claude subscription | Pro / Max plan | ~$0 extra (counts against plan quota) |
| Codex subscription | OpenAI Plus | ~$0 extra (counts against plan quota) |
| Claude API | ~$15/$75 per 1M tokens (Opus 4.7) | ~$2–8 |
| OpenAI API | ~$5/$15 per 1M tokens (GPT-4o-class) | ~$1–4 |
| Gemini API | ~$1.25/$10 per 1M tokens (2.5 Pro) | ~$0.5–2 |
| OpenRouter API | Model-dependent | Varies |

In `api` orchestration mode (`--with-api`), add ~$0.50–1.00 per feature for planner + evaluator overhead billed to `ANTHROPIC_API_KEY`. In default `runner` mode this overhead is zero.
