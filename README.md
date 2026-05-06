# Agent Harness

A production orchestration framework for long-running AI coding agents, implementing the design patterns from Anthropic's engineering articles:

- [Harness Design for Long-Running Application Development](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

Supports **7 interchangeable runners** and **2 orchestration modes** — run everything on your Claude/OpenAI subscription with no API key, or mix-and-match subscription generators with API-powered planners and evaluators.

---

## Quick Start

```bash
# 1. Create conda environment
conda create -n harness python=3.12 -y && conda activate harness
pip install -e ".[all-providers]"

# 2. Start a new project — interactive, no YAML needed
harness new --claude-code        # pure subscription, no API key required
harness new --claude-sdk         # same but via SDK (structured output)
harness new --openai-api         # pay-per-token, needs OPENAI_API_KEY
harness new                      # shows interactive runner menu

# 3. Resume an existing project
harness run output/my_app_a3f8c21b/config.yaml

# 4. Other commands
harness runners                  # list all runners with requirements
harness status output/my_app_a3f8c21b/config.yaml
```

---

## Architecture

```
harness new
  │
  ├─ Prompts: project name, brief
  ├─ Requirement Alignment (planner ↔ user, back-and-forth until confirmed)
  ├─ Runner selection (flag / menu)
  └─ Saves config to ./output/<slug>_<id>/config.yaml
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
| **Pluggable runners** | Swap execution engine without changing orchestration logic |

---

## Runners

The **runner** is what executes implementation work. Pick the one that fits your setup:

| Runner flag | Internal name | Billing | File I/O | Requires |
|-------------|---------------|---------|----------|----------|
| `--claude-code` | `subprocess` | Claude subscription | ✅ Full | `claude` CLI |
| `--claude-sdk` | `sdk` | Claude subscription | ✅ Full | `pip install -e ".[sdk]"` |
| `--codex` | `codex` | OpenAI subscription | ✅ Full | `codex` CLI |
| `--anthropic-api` | `anthropic` | Pay-per-token | ❌ Text only | `ANTHROPIC_API_KEY` |
| `--openai-api` | `openai` | Pay-per-token | ❌ Text only | `OPENAI_API_KEY` |
| `--gemini` | `gemini` | Pay-per-token | ❌ Text only | `GEMINI_API_KEY` |
| `--openrouter` | `openrouter` | Pay-per-token | ❌ Text only | `OPENROUTER_API_KEY` |

**Agentic runners** (`--claude-code`, `--claude-sdk`, `--codex`) write files, run shell commands, and commit git — they use your existing subscription.

**API runners** stream text output only. The model describes what it would build; no files are written automatically.

---

## Orchestration Modes

The **orchestration mode** controls how the **planner** and **evaluator** agents run (the generator always uses the runner you selected):

| Mode | Planner + Evaluator | API key needed? | Set via |
|------|--------------------|-----------------|----|
| `runner` | Same runner as generator | **No** — pure subscription | Default for agentic runners |
| `api` | Anthropic API directly | **Yes** — `ANTHROPIC_API_KEY` | Default for API runners, or `--with-api` |

```bash
# Pure subscription — no API key at all
harness new --claude-code

# Split: generator = Claude Code CLI, planner/evaluator = Anthropic API
harness new --claude-code --with-api

# All API — ANTHROPIC_API_KEY required, plus OPENAI_API_KEY
harness new --openai-api           # api mode is the only option for API runners
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

---

## Starting a New Project

`harness new` replaces manual YAML editing with an interactive flow:

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

Config saved to ./output/my_todo_app_a3f8c21b/config.yaml
```

The confirmed spec is injected directly into the orchestrator, skipping an extra planner API call.

---

## Resuming a Project

```bash
harness run output/my_todo_app_a3f8c21b/config.yaml

# Override the runner (e.g. switch from SDK to subprocess)
harness run output/my_todo_app_a3f8c21b/config.yaml --runner subprocess
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
│   │   ├── subprocess_runner.py # claude --print       (subscription)
│   │   ├── sdk_runner.py        # claude_code_sdk      (subscription)
│   │   ├── codex_runner.py      # codex CLI            (OpenAI subscription)
│   │   ├── api_runner.py        # Anthropic API        (pay-per-token)
│   │   ├── openai_api_runner.py # OpenAI API           (pay-per-token)
│   │   ├── gemini_api_runner.py # Google Gemini        (pay-per-token)
│   │   └── openrouter_api_runner.py # OpenRouter       (pay-per-token)
│   ├── context/
│   │   ├── handoff.py           # HandoffDocument: cross-session state transfer
│   │   └── reset.py             # Token budget tracking + context reset logic
│   ├── progress/
│   │   ├── models.py            # Feature, ProjectProgress, EvaluationResult (Pydantic)
│   │   └── tracker.py           # Read/write features.json + progress.md
│   ├── session/
│   │   └── opener.py            # Session startup context builder
│   ├── config.py                # HarnessConfig: YAML-backed, includes orchestration_mode
│   └── orchestrator.py          # Main loop: phases, GAN loop, context resets, runner status
├── tests/
│   ├── test_runners.py          # 33 runner tests (factory, preflight, all 7 runners)
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

Saved automatically to `output/<slug>_<id>/config.yaml` by `harness new`. You can also write it manually.

```yaml
project_name: "my-app"
project_id: "a3f8c21b"          # auto-generated, used to name the output directory
brief: "One to four sentences describing what to build."
output_dir: "./output/my_app_a3f8c21b"

# Orchestration mode:
#   "runner" — planner + evaluator use the same runner (no API key for subscription runners)
#   "api"    — planner + evaluator call Anthropic API directly (ANTHROPIC_API_KEY required)
orchestration_mode: "runner"

# Runner selection
# Options: subprocess | sdk | codex | anthropic | openai | gemini | openrouter
code_runner: "subprocess"

# API keys for non-Anthropic runners (can also be set as env vars)
openai_api_key: null      # or OPENAI_API_KEY
gemini_api_key: null      # or GEMINI_API_KEY
openrouter_api_key: null  # or OPENROUTER_API_KEY

# Models (used by API runners and api orchestration mode)
planner_model: "claude-opus-4-7"
generator_model: "claude-opus-4-7"
evaluator_model: "claude-opus-4-7"

# GAN loop
max_iterations_per_feature: 15
evaluator_pass_score: 8.0       # out of 10
sprint_contract_enabled: true

# Evaluator rubric weights (must sum to 1.0)
evaluator_weights:
  design_quality: 0.30
  originality: 0.30
  craft: 0.25
  functionality: 0.15

# Context management
context_reset_threshold_tokens: 150000
```

---

## Environment Variables

| Variable | Required when |
|----------|--------------|
| `ANTHROPIC_API_KEY` | `orchestration_mode: api` or `--anthropic-api` runner |
| `OPENAI_API_KEY` | `--openai-api` or `--openrouter` runner |
| `GEMINI_API_KEY` | `--gemini` runner |
| `OPENROUTER_API_KEY` | `--openrouter` runner |

With `--claude-code`, `--claude-sdk`, or `--codex` in the default runner mode, **no API key is needed**.

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
python -m pytest tests/ -v          # 57 tests, no API key needed — all mocked
python -m pytest tests/test_runners.py -v   # runner-specific (33 tests)
```

---

## Cost Reference

| Runner | Billing model | Typical cost per feature (5 iterations) |
|--------|--------------|----------------------------------------|
| `subprocess` / `sdk` | Claude subscription | ~$0 extra |
| `codex` | OpenAI subscription | ~$0 extra |
| `anthropic` | ~$15/$75 per 1M tokens (Opus 4.7) | ~$2–8 |
| `openai` | ~$5/$15 per 1M tokens (GPT-4o) | ~$1–4 |
| `gemini` | ~$1.25/$10 per 1M tokens (2.5 Pro) | ~$0.5–2 |
| `openrouter` | model-dependent | varies |

In `api` orchestration mode, add ~$0.50–1.00 per feature for planner + evaluator overhead billed to `ANTHROPIC_API_KEY`. In `runner` mode this overhead is zero.
