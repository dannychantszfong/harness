# Agent Harness

A production orchestration framework for long-running AI coding agents, implementing the design patterns from Anthropic's engineering articles:

- [Harness Design for Long-Running Application Development](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

**One paragraph in, a working project out:** a planner expands your brief into a spec (you confirm it interactively), an initializer turns the spec into an ordered list of atomic features, and every feature goes through a generator ↔ evaluator loop until a weighted score clears the bar. State lives in files, so a run can be killed and resumed at any point; usage caps are detected and the run resumes itself after the reset. Everything runs through a coding-agent runtime — **Claude Code** or **Codex** — so a subscription is the only requirement.

---

## Quick Start

```bash
# 1. Install (Python 3.11+). Any virtualenv or conda env works.
pip install .                       # or: pip install -e ".[dev]" for development
pip install ".[sdk]"                # optional: the Claude Code SDK runner

# 2. Optional, once: runner rotation policy + tool check (gh, claude, codex)
harness setup

# 3. Start a project — interactive, no config editing needed
harness new --claude-code           # Claude Code CLI on your Claude subscription
harness new --codex --model gpt-5.2 # Codex CLI on your OpenAI subscription
harness new                         # interactive runner menu

# 4. Pick up where it stopped — from any working directory
harness resume output/my_app_a3f8c21b
harness status output/my_app_a3f8c21b

# 5. Bring an existing repo in at the right phase
harness import ../my-other-repo
```

You need the coding-agent CLI for the runner you pick (`claude` or `codex`) on `PATH`, signed in. `gh` is needed only for per-project GitHub sync.

---

## How it works

```
harness new
  ├─ project name + brief
  ├─ Requirement alignment: planner drafts a spec ↔ you give feedback, until confirmed
  └─ output/<slug>_<id>/  (its own git repo)  ← harness_config.json, spec.md

Phase 1 — Initializer   features.json (50–200 atomic features), init script, first commit
Phase 2 — Planner       skipped when the spec was confirmed during `harness new`
Phase 3 — Feature loop, one feature at a time, highest priority first:
  ┌──────────────────────────────────────────────────────────────────────┐
  │ Sprint contract: acceptance criteria + out of scope (read-only call) │
  │ Generator implements the feature in the project dir                  │
  │ Evaluator inspects + grades (read-only session)                      │
  │   Harness computes the weighted score                                │
  │   pass = score ≥ pass_score AND functionality ≥ floor                │
  │ pass → Harness commits "feat(<id>): <name> — score <x>" → next       │
  │ fail → feedback to the generator → iterate (budget per feature)      │
  │ budget exhausted → attempt kept on a branch, code reverted, notes    │
  └──────────────────────────────────────────────────────────────────────┘
  Context budget exceeded (real token counts) → handoff file → fresh session
  Usage cap hit → rotate to the next runner profile, or schedule auto-resume
```

### Key design decisions

Every row is active in the default configuration.

| Decision | What Harness does |
|---|---|
| **Context resets over compaction** | Token usage reported by Claude Code / Codex is counted; past `context_reset_threshold_tokens` Harness writes `handoff_session_NNNN.json` and the next session starts from it (ADR-001). |
| **Separate generator and evaluator** | The evaluator is a different session, read-only, told not to trust the generator's self-report (ADR-002). |
| **Harness owns the verdict** | The overall score is computed by Harness from the four sub-scores and your weights; a feature that does not work (functionality below the floor) never passes, however pretty it is (ADR-013). |
| **One feature per session** | The generator is told to work only on the named feature (ADR-003). |
| **File-based, restartable state** | `features.json` is the source of truth. Interrupted features resume first, failing features are retried while budget remains, a corrupt file is backed up and never overwritten (ADR-004). |
| **Sprint contracts** | Before each feature, the generator proposes acceptance criteria and out-of-scope items; the evaluator grades against them (ADR-006). |
| **Recovery point per feature** | Harness commits every passing feature itself. An exhausted feature's code is preserved on `harness/attempt/<id>/<n>` and the working branch is reset to the last stable state; later attempts get the lessons (ADR-015). |
| **Two coding agents, subscription first** | All execution flows through Claude Code or Codex; API providers plug in as the model (ADR-008). |
| **Independent output repos** | The Harness repo ignores `output/`; every project owns its git repo and optional GitHub remote. |

---

## Coding agents and billing modes

| Agent | Runner | Notes |
|---|---|---|
| **Claude Code** | `subprocess` | `claude -p --output-format json`; the default |
| **Claude Code** | `sdk` | Python `claude-code-sdk` (pinned 0.0.25, ADR-014); streams tool calls |
| **Codex** | `codex` | `codex exec --json` |

| # | Mode | Agent | Auth | Status |
|---|---|---|---|---|
| 1 | Claude subscription | Claude Code | Pro / Max plan, no env var | default for `--claude-code` / `--claude-sdk` |
| 2 | Claude API | Claude Code | `ANTHROPIC_API_KEY` | |
| 3 | Codex subscription | Codex | signed-in Codex CLI | default for `--codex` |
| 4 | OpenAI API | Codex | `OPENAI_API_KEY` | |
| 5 | Gemini API | Codex (custom provider) or Claude Code via OpenRouter | `GEMINI_API_KEY` | documented, **not verified** |
| 6 | OpenRouter | Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY` | |

Harness does not export these variables; set them in your shell (or `direnv`). `--model` picks the model inside the agent (`harness new --claude-code --model sonnet`); it is saved as `code_runner_model`.

### Runner rotation (`harness setup`)

`harness setup` verifies `gh` and the coding-agent CLIs, then saves a policy to `~/.harness/setup.json` that new projects copy:

```bash
harness setup \
  --profile claude:subprocess:sonnet \
  --profile codex:codex:gpt-5.2 \
  --profile claude-openrouter:subprocess:anthropic/claude-sonnet-4-6:openrouter \
  --profile-env claude-openrouter:ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1 \
  --profile-env 'claude-openrouter:ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY' \
  --generator-order claude,codex,claude-openrouter \
  --evaluator-order codex,claude-openrouter
```

When a runner reports a usage cap, the role moves to the next profile in its order (ADR-011). Before the first feature, Harness preflights the first profile of every role in use, so a missing binary fails immediately rather than mid-run. A profile only inherits `code_runner_model` when it uses the same runtime: an `opus` alias is never handed to Codex.

### Orchestration modes

| Mode | Planner + evaluator | API key | How |
|---|---|---|---|
| `runner` (default) | the coding-agent runner | none | default |
| `api` | Anthropic Messages API | `ANTHROPIC_API_KEY` | `harness new --with-api` |

In `api` mode the evaluator still inspects the project: it runs a bounded tool loop (`read_file`, `list_files`, `search`, and `run_command` for the startup command, your `test_command`, and `git diff/log/status/show`), at most `api_tool_rounds` rounds, before it must submit scores (ADR-016). `harness setup` never switches a project you created in `api` mode back to `runner` mode.

---

## Evaluation

Four dimensions, each scored 1–10: design quality, originality, craft, functionality. Defaults:

| Setting | Default | Meaning |
|---|---|---|
| `evaluator_weights` | 0.30 / 0.30 / 0.25 / 0.15 | must sum to 1.0, otherwise the config fails to load |
| `evaluator_pass_score` | 8.0 | weighted score needed to pass |
| `evaluator_functionality_floor` | 6.0 | functionality needed to pass, whatever the weighted score |
| `max_iterations_per_feature` | 15 | budget per feature, across runs |

The weights in the evaluator's prompt are the configured ones. Out-of-range scores are clamped to 1–10. If the evaluator's answer cannot be read, that is recorded as an explicit "no usable grade" (never a silent 5.0), and Harness asks once more before counting the iteration.

**When a feature cannot pass.** After the budget is used up, Harness commits the attempt onto `harness/attempt/<feature-id>/<n>` and writes `.harness/attempts/<feature-id>.md` (every score, all feedback, the weakest dimension, the generator's last self-evaluation). It then resets the working branch to the commit the feature started from, so the next feature builds on a passing baseline. The final panel, `progress.md` and `harness status` list exhausted features. Raise `max_iterations_per_feature` or edit the feature's description, then run `harness resume`: the retry gets the notes injected as lessons.

---

## Resuming, auto-resume and state

```bash
harness resume output/my_app_a3f8c21b      # works from any directory
```

`harness resume` prints what it is about to do: passing, in-progress, pending and exhausted counts, the feature it picks up (and at which iteration), and whether a handoff applies. The directory you pass is the project; a relative `output_dir` recorded by an older Harness is corrected and saved.

| Situation | What happens |
|---|---|
| Killed mid-feature | the interrupted feature resumes first, with its last evaluator feedback |
| Feature failed, budget left | retried, continuing its iteration count |
| `features.json` corrupt | copied to `features.json.corrupt-<timestamp>`; the run stops and nothing is overwritten |
| An agent edits `features.json` | restored from Harness's copy, with a warning |
| Usage cap with a reset time | a one-shot OS job runs `harness resume <dir>` after the reset (`auto_resume_on_rate_limit`) |

Auto-resume backends: **macOS launchd**; **Windows Task Scheduler** (validated on a real host); **Linux systemd user timer** (experimental, needs a user D-Bus session; Harness says so instead of failing silently). The scheduled wrappers carry the `PATH` from scheduling time, never secrets. See the [runbook](docs/operational/runbook.md#auto-resume-backends).

## Tokens and cost

Every runner session's usage (Claude Code JSON output, Codex event stream, SDK result) is added to the feature and project totals:

```
$ harness status output/my_app_a3f8c21b
Progress: 40.0% (4/10 features)
Usage: 1,284,113 tokens · cost: subscription (≈$18.40 at API prices)
  ✅ [f1] User sign-up (8.6/10 · 212,904 tok, $3.10)
```

On a subscription the dollar figure is the runtime's API-price equivalent, not what you are billed. Tokens are new context (uncached input, cache writes, output); cache reads are not counted toward the reset budget.

---

## Safety

Read this before an unattended run.

- **The generator and initializer run with permissions bypassed** (`claude --dangerously-skip-permissions`, `codex --dangerously-bypass-approvals-and-sandbox`) inside the project directory. They can run any command your user can. For unattended runs, use a container, a VM or a dedicated OS user without access to your other files and credentials.
- **Planner, evaluator and sprint-contract sessions are read-only** by default (`read_only_roles`): on Claude Code, `--permission-mode dontAsk` with an allow-list (Read/Grep/Glob, plus the startup command, `test_command` and `git diff/log/status/show`) and Write/Edit disallowed; on Codex, `--sandbox read-only`. Tune per role with `role_allowed_tools`.
- **No literal secrets in `harness_config.json`.** It is committed to the project repo and may be pushed to GitHub. API-key fields and secret-looking env values (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`) must be references such as `"$OPENROUTER_API_KEY"`; a literal value is rejected on load and on save.
- Harness keeps `.auto_resume.*` and `auto_resume.log` out of the project's commits, and git operations only ever run in the project's own repository root.

---

## Per-project GitHub sync

```bash
harness new --claude-code --github-repo owner/my-app          # gh creates/reuses the repo
harness import ../my-app --github-repo owner/my-app            # push the imported copy
harness new --codex --git-remote git@github.com:owner/my-app.git
harness new --claude-code --github-repo owner/my-app --no-git-push
```

Harness pushes after initialization and after each passing feature. `--github-repo` needs `gh` installed and authenticated; a missing or unauthenticated `gh` stops the run, while network hiccups are warnings.

---

## Experimental

- **Browser verification** (`evaluator_browser_verification: true`): runner-mode evaluator sessions on Claude Code get a Playwright MCP server (`npx -y @playwright/mcp@latest`, or your own `evaluator_mcp_config`) and are told to exercise the acceptance criteria in the running app. Preflight checks the server command is on `PATH`. Codex evaluators run without it, with a warning. Not part of the 1.0 guarantee.
- **systemd auto-resume backend**: see above.

---

## Configuration reference

`harness new` writes `output/<slug>_<id>/harness_config.json`. To write one by hand, a minimal YAML is enough (see `examples/`):

```yaml
project_name: my-app
brief: A task manager with projects, due dates and a Kanban board.
output_dir: ./output/my_app
orchestration_mode: runner      # the default
code_runner: subprocess         # subprocess | sdk | codex
```

```bash
harness run examples/web_app.yaml
```

All fields, with defaults:

```json
{
  "project_name": "my-app",
  "brief": "One to four sentences describing what to build.",
  "output_dir": "/home/me/harness/output/my_app_a3f8c21b",
  "project_id": "a3f8c21b",
  "orchestration_mode": "runner",
  "code_runner": "subprocess",
  "code_runner_model": "sonnet",
  "codex_oss": false,
  "codex_local_provider": null,
  "code_runner_extra_args": [],
  "active_runner_env": {},

  "runner_timeout_seconds": 1800,
  "planner_timeout_seconds": null,
  "evaluator_timeout_seconds": null,
  "sdk_max_turns": 100,

  "max_iterations_per_feature": 15,
  "evaluator_pass_score": 8.0,
  "evaluator_weights": {"design_quality": 0.3, "originality": 0.3, "craft": 0.25, "functionality": 0.15},
  "evaluator_functionality_floor": 6.0,
  "sprint_contract_enabled": true,
  "context_reset_threshold_tokens": 150000,

  "read_only_roles": ["planner", "evaluator", "contract"],
  "role_allowed_tools": {},
  "test_command": null,
  "evaluator_browser_verification": false,
  "evaluator_mcp_config": null,
  "api_tool_rounds": 20,
  "api_command_timeout_seconds": 120,

  "features_file": "features.json",
  "progress_file": "progress.md",
  "spec_file": "spec.md",
  "init_script": null,
  "init_script_type": null,
  "startup_command": null,

  "auto_resume_on_rate_limit": true,
  "runner_profiles": [],
  "planner_runner_order": [],
  "generator_runner_order": [],
  "evaluator_runner_order": [],
  "reviewer_runner_order": [],
  "fallback_on_rate_limit": true,

  "project_git_push": false,
  "project_git_branch": "main",
  "project_git_remote": null,
  "project_github_repo": null,
  "project_github_private": true,

  "progress_animation": "sparkle",
  "progress_phrase_style": "playful",
  "progress_text_effect": "typewriter",

  "planner_model": "claude-opus-4-7",
  "generator_model": "claude-opus-4-7",
  "evaluator_model": "claude-opus-4-7",
  "openai_api_key": null,
  "gemini_api_key": null,
  "openrouter_api_key": null
}
```

`planner_model` / `generator_model` / `evaluator_model` apply to `api` orchestration mode; runner mode uses `code_runner_model`. `output_dir` is always saved as an absolute path. Most fields can be edited while a run is going: Harness re-reads the file between features and between iterations (identity fields such as `output_dir` and `code_runner` are pinned).

---

## Other commands

```bash
harness runners                                   # runners and requirements
harness init <config> "<brief>"                   # initializer only
harness plan <config>                             # planner only
harness animation-theme "moonlit ritual" --runner codex   # re-theme the wait animation
```

The wait animation (`progress_animation`, `progress_phrase_style`, `progress_text_effect`) renders only in interactive terminals; disable it with `HARNESS_NO_SPINNER=1`.

---

## Development

```bash
pip install -e ".[dev]"
ruff check .
python -m pytest -q                                    # no claude/codex/gh/API key needed
HARNESS_PACKAGING_TESTS=1 python -m pytest tests/test_packaging.py   # wheel → clean venv → harness --help
```

CI (`.github/workflows/ci.yml`) runs lint, the suite with the coding-agent and GitHub CLIs removed from `PATH`, and the packaging check on Python 3.11 and 3.12, on Ubuntu and macOS.

## Project structure

```
harness/
├── cli.py                  # `harness` entry point (harness.cli:main)
├── config.py               # HarnessConfig, RunnerProfile, secret checks
├── orchestrator.py         # phases, GAN loop, recovery commits, resets, rotation
├── agents/
│   ├── base.py             # API calls, runner calls, api-mode tool loop, permissions
│   ├── initializer.py      # features.json + init script + first commit
│   ├── planner.py          # brief → spec; interactive alignment
│   ├── generator.py        # sprint contract + feature implementation
│   ├── evaluator.py        # scoring: parse, clamp, weights, floor
│   ├── reviewer.py         # REVIEW.md for imported / finished repos
│   ├── tools.py            # api-mode inspection tools (read/list/search/run)
│   └── browser.py          # experimental Playwright MCP for the evaluator
├── runners/                # subprocess (claude), sdk, codex; rate-limit parsing
├── progress/               # models, tracker (atomic saves, state guard), attempts notes
├── context/                # handoff documents, reset budget
├── session/opener.py       # session preamble + startup checklist
├── auto_resume.py          # launchd / systemd / Task Scheduler
├── project_git.py          # recovery commits, attempt branches, GitHub sync
├── import_repo.py          # `harness import` stage detection
├── preflight.py            # `harness setup` tool checks
├── runner_profiles.py      # setup.json, role fallback chains
├── timeutil.py             # UTC helpers
└── ui/spinner.py           # wait animation
tests/                      # pytest suite (see conftest.py: no real binaries)
docs/                       # PRD, ADRs, architecture, runbook, test plan, diagrams
examples/                   # web_app.yaml, game.yaml
```
