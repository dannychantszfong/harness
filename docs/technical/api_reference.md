# API Reference — Claude Agent Harness

**Version:** 2.0  
**Last updated:** 2026-05-06

---

## CLI Commands

### `harness setup`

Configure first-time runner rotation. The saved policy is copied into new/imported projects and can also be applied on resume when a project has no local policy yet.

```bash
harness setup

harness setup \
  --profile claude:subprocess:sonnet \
  --profile codex:codex:gpt-5.2 \
  --profile claude-openrouter:subprocess:anthropic/claude-sonnet-4-6:openrouter \
  --profile-env claude-openrouter:ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1 \
  --profile-env 'claude-openrouter:ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY' \
  --planner-order codex,claude \
  --generator-order claude,codex,claude-openrouter \
  --evaluator-order codex,claude-openrouter

harness setup --show
```

**Options:**

| Option | Description |
|--------|-------------|
| `--profile name:runner[:model[:provider]]` | Define a named runner profile |
| `--profile-env profile:KEY=VALUE` | Add env vars for a profile; `$ENV_NAME` expands at runtime |
| `--profile-extra-arg profile:ARG` | Add one extra CLI argument for that profile |
| `--planner-order` | Comma-separated whitelist/priority list for planning |
| `--generator-order` | Comma-separated whitelist/priority list for coding |
| `--evaluator-order` | Comma-separated whitelist/priority list for evaluation |
| `--reviewer-order` | Comma-separated whitelist/priority list for whole-project review |
| `--no-fallback-on-rate-limit` | Disable automatic rotation on usage caps |

---

### `harness new`

Create a new output project interactively, confirm a product spec, then start the full workflow.

```bash
harness new --claude-code
harness new --codex --model gpt-5.2
harness new --claude-code --github-repo owner/my-app
harness new --codex --git-remote git@github.com:owner/my-app.git
```

**GitHub options:**

| Option | Description |
|--------|-------------|
| `--github-repo owner/repo` | Create/check that GitHub repo with `gh`, then push the output project after init and each passing feature |
| `--git-remote URL` | Use an existing remote URL as `origin` for the output project |
| `--github-private` / `--github-public` | Visibility if `--github-repo` creates the repo |
| `--no-git-push` | Save the remote settings without auto-pushing during the run |

Harness itself ignores `output/`; each generated project is initialized and pushed as its own independent git repo.

---

### `harness import <source_path>`

Copy or harness-ify an existing local repository, detect its stage, and enter the matching workflow phase.

```bash
harness import ../my-existing-app
harness import ../my-existing-app --github-repo owner/my-existing-app
harness import ../my-existing-app --git-remote git@github.com:owner/my-existing-app.git
harness import ../my-existing-app --in-place
```

By default, import copies the source into `output/<slug>_<id>/` without the source `.git/` directory, so the imported project copy becomes a fresh independent repo. `--in-place` keeps the source repo in place and preserves its current branch.

---

### `harness run <config_file>`

Run the full harness end-to-end (Initialize → Plan → Feature Loop).

```bash
# Prompted to select runner interactively
harness run output/web_app_a3f8c21b/harness_config.json

# Skip prompt with --runner / -r
harness run output/web_app_a3f8c21b/harness_config.json --runner subprocess
harness run output/web_app_a3f8c21b/harness_config.json -r sdk
harness run output/web_app_a3f8c21b/harness_config.json -r codex
```

**Arguments:**

| Argument | Type | Description |
|----------|------|-------------|
| `config_file` | path | Harness config file, usually `harness_config.json` |

**Options:**

| Option | Short | Choices | Description |
|--------|-------|---------|-------------|
| `--runner` | `-r` | See below | Runner to use — skips interactive prompt |

**Runner choices:** `subprocess` `sdk` `codex`

API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not standalone runners — they plug into one of the three above via env vars (see Environment Variables below).

**Runner selection priority:** `--runner` flag > `code_runner` in config > interactive prompt

---

### `harness runners`

List the three coding-agent runners in a formatted table. Does not start a run.

```bash
harness runners
```

```
┏━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ Runner     ┃ Family  ┃ Billing             ┃ File I/O ┃ Requires             ┃
┡━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ subprocess │ Agentic │ Claude subscription │ ✅ full  │ `claude` CLI         │
│ sdk        │ Agentic │ Claude subscription │ ✅ full  │ pip install …        │
│ codex      │ Agentic │ OpenAI subscription │ ✅ full  │ `codex` CLI          │
└────────────┴─────────┴─────────────────────┴──────────┴──────────────────────┘
```

The four direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) plug into one of the three runners via env vars and are not separately listed.

---

### `harness animation-theme <theme>`

Ask a signed-in coding agent to rewrite the playful quiet-animation verbs in the local Harness checkout.

```bash
harness animation-theme "frostbound library" --runner codex --model gpt-5.2
harness animation-theme "quiet moonlit ritual" --runner subprocess
```

The command is intentionally limited to agentic runners because the task requires file edits.

**Arguments:**

| Argument | Type | Description |
|----------|------|-------------|
| `theme` | text | Theme or mood to translate into animation verbs |

**Options:**

| Option | Short | Choices | Description |
|--------|-------|---------|-------------|
| `--runner` | `-r` | `subprocess`, `sdk`, `codex` | Signed-in coding agent to use |
| `--model` | | text | Optional Claude Code/Codex model override |
| `--timeout` | | seconds | Maximum wait for the theme edit |

The invoked agent follows `docs/technical/animation_theme_agent_guide.md`, editing `PHRASES["playful"]` in `harness/ui/spinner.py` with short single verbs only.

---

### `harness status <config_file>`

Print current project progress. No API calls.

```bash
harness status output/web_app_a3f8c21b/harness_config.json
```

---

### `harness init <config_file> <brief>`

Run only the initialization phase.

```bash
harness init output/web_app_a3f8c21b/harness_config.json "A todo app with Kanban view"
```

**Options:** `--project-name TEXT`

---

### `harness plan <config_file>`

Run only the planner agent to expand the brief into a spec.

```bash
harness plan output/web_app_a3f8c21b/harness_config.json
```

---

## Python API

### `HarnessConfig`

```python
from harness.config import HarnessConfig

config = HarnessConfig.from_file("output/web_app_a3f8c21b/harness_config.json")
```

**Config fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `project_name` | str | required | Project identifier |
| `brief` | str | required | 1–4 sentence project description |
| `output_dir` | str | `"./output"` | Where files are written |
| `code_runner` | str\|None | `None` | Runner to use (prompts if None) |
| `planner_model` | str | `"claude-opus-4-7"` | Model for PlannerAgent in `orchestration_mode='api'` |
| `generator_model` | str | `"claude-opus-4-7"` | Reserved for `orchestration_mode='api'` |
| `evaluator_model` | str | `"claude-opus-4-7"` | Model for EvaluatorAgent in `orchestration_mode='api'` |
| `code_runner_model` | str\|None | `None` | Model passed to Claude Code / Codex as `--model` |
| `runner_profiles` | list | `[]` | Named runner/model/env profiles used for role-aware rotation |
| `planner_runner_order` | list[str] | `[]` | Planner profile whitelist and priority order |
| `generator_runner_order` | list[str] | `[]` | Generator profile whitelist and priority order |
| `evaluator_runner_order` | list[str] | `[]` | Evaluator profile whitelist and priority order |
| `reviewer_runner_order` | list[str] | `[]` | Reviewer profile whitelist and priority order |
| `fallback_on_rate_limit` | bool | `True` | Move to the next role profile when the current runner reports a usage cap |
| `openai_api_key` | str\|None | `None` | Documents the OpenAI key the project expects (set `OPENAI_API_KEY` env var to use it) |
| `gemini_api_key` | str\|None | `None` | Documents the Gemini key the project expects (set `GEMINI_API_KEY` env var) |
| `openrouter_api_key` | str\|None | `None` | Documents the OpenRouter key the project expects (set `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`) |
| `project_git_push` | bool | `False` | Auto-push this output project repo during workflow seams |
| `project_git_branch` | str | `"main"` | Branch name for newly initialized output repos |
| `project_git_remote` | str\|None | `None` | Existing git remote URL to set as `origin` |
| `project_github_repo` | str\|None | `None` | GitHub `owner/repo`; checked/created with `gh` when no remote URL is set |
| `project_github_private` | bool | `True` | Visibility used when creating `project_github_repo` |
| `max_iterations_per_feature` | int | `15` | Max GAN loop iterations |
| `evaluator_pass_score` | float | `8.0` | Minimum score to mark PASSING |
| `context_reset_threshold_tokens` | int | `150_000` | Token count triggering a reset |
| `sprint_contract_enabled` | bool | `True` | Negotiate contracts before implementation |
| `evaluator_weights.design_quality` | float | `0.30` | |
| `evaluator_weights.originality` | float | `0.30` | |
| `evaluator_weights.craft` | float | `0.25` | |
| `evaluator_weights.functionality` | float | `0.15` | |

---

### `Orchestrator`

```python
from harness import Orchestrator, HarnessConfig
from harness.runners import RunnerType

config = HarnessConfig.from_file("output/web_app_a3f8c21b/harness_config.json")

# Runner from config/prompt
orchestrator = Orchestrator(config)

# Explicit runner
orchestrator = Orchestrator(config, runner_type=RunnerType.SUBPROCESS)
orchestrator = Orchestrator(config, runner_type=RunnerType.CODEX)

orchestrator.run()
```

---

### `CodeRunner` and `RunResult`

```python
from harness.runners import create_runner, RunnerType, RunResult

runner = create_runner(RunnerType.SUBPROCESS, config)

result: RunResult = runner.implement(
    prompt="Implement the login feature...",
    cwd="/path/to/project",
    timeout_seconds=600,
)

print(result.success)          # bool
print(result.output)           # self-evaluation text
print(result.error)            # None if success
print(result.input_tokens)     # int | None (populated by SDK runner)
print(result.output_tokens)    # int | None
print(result.cost_usd)         # float | None
print(result.tool_calls_observed)  # list[str] (SDK runner only)
print(result.rate_limit_reset_at)  # datetime | None (set if a subscription cap is hit)
```

---

### Using runners directly

```python
from harness.runners import SubprocessRunner, SDKRunner, CodexRunner

# Claude Code CLI
runner = SubprocessRunner(config)
result = runner.implement(prompt, cwd="/my/project")

# Claude Code via OpenRouter (set env vars BEFORE constructing the runner)
import os
os.environ["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api/v1"
os.environ["ANTHROPIC_AUTH_TOKEN"] = os.environ["OPENROUTER_API_KEY"]
config.code_runner_model = "anthropic/claude-sonnet-4-6"
runner = SubprocessRunner(config)
result = runner.implement(prompt, cwd="/my/project")
```

---

## JSON Config Reference

```json
{
  "project_name": "my-web-app",
  "project_id": "a3f8c21b",
  "brief": "A task management web app with projects, tasks, due dates, priorities, completion, and a Kanban board.",
  "output_dir": "./output/web_app_a3f8c21b",
  "orchestration_mode": "runner",
  "code_runner": "subprocess",
  "code_runner_model": null,
  "codex_oss": false,
  "codex_local_provider": null,
  "code_runner_extra_args": [],
  "openai_api_key": null,
  "gemini_api_key": null,
  "openrouter_api_key": null,
  "planner_model": "claude-opus-4-7",
  "generator_model": "claude-opus-4-7",
  "evaluator_model": "claude-opus-4-7",
  "max_iterations_per_feature": 15,
  "evaluator_pass_score": 8.0,
  "evaluator_weights": {
    "design_quality": 0.30,
    "originality": 0.30,
    "craft": 0.25,
    "functionality": 0.15
  },
  "context_reset_threshold_tokens": 150000,
  "sprint_contract_enabled": true,
  "progress_animation": "sparkle",
  "progress_phrase_style": "playful",
  "progress_text_effect": "typewriter"
}
```

---

## Environment Variables

| Variable | Mode | Description |
|----------|---------|-------------|
| *(none)* | Claude / Codex subscription | Default — uses your signed-in plan |
| `ANTHROPIC_API_KEY` | Claude API, or `--with-api` orchestration | Pay-per-token Anthropic auth for Claude Code; also required for `orchestration_mode='api'` planner+evaluator |
| `OPENAI_API_KEY` | OpenAI API | Pay-per-token OpenAI auth for Codex |
| `GEMINI_API_KEY` | Gemini API | Routed via Codex custom provider or OpenRouter through Claude Code |
| `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | OpenRouter API | Point Claude Code at OpenRouter's Anthropic-compatible endpoint; `ANTHROPIC_AUTH_TOKEN` should equal your `OPENROUTER_API_KEY` |

The harness does not auto-export these — set them in your shell, in `direnv`, or in your project's `.env` so Claude Code / Codex pick them up.
