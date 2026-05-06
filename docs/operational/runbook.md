# Runbook — Claude Agent Harness

**Audience:** Engineers running or debugging the harness  
**Version:** 2.0  
**Last updated:** 2026-05-06

---

## Prerequisites

```bash
# 1. Create and activate the conda environment
conda create -n harness python=3.12 -y
conda activate harness

# 2. Install core harness
pip install -e .

# 3. Install Claude Code SDK transport (optional)
pip install -e ".[sdk]"

# 4. Install the coding-agent CLIs you intend to use:
#    - Claude Code: https://claude.ai/download
#    - Codex CLI:   https://github.com/openai/codex
#
#    Optional for --github-repo project sync:
#    - GitHub CLI:  https://cli.github.com/
#      Run: gh auth login

# 5. Set env vars only for the mode you want
export ANTHROPIC_API_KEY=sk-ant-...    # Claude API mode (or --with-api)
export OPENAI_API_KEY=sk-...           # OpenAI API mode (Codex)
export GEMINI_API_KEY=AIza...          # Gemini API mode
# OpenRouter mode (route Claude Code through OpenRouter):
export ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1
export ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY

# 6. Verify
harness --help
harness runners   # confirm the three coding-agent rows print cleanly

# Optional first-time policy
harness setup
```

---

## Standard Operating Procedure: New Project

```bash
conda activate harness

# Step 1 — Create config
cp output/web_app_a3f8c21b/harness_config.json output/my_project_a3f8c21b/harness_config.json
# Edit: project_name, brief, output_dir, code_runner (optional)

# Step 2 — Initialize
harness init output/my_project_a3f8c21b/harness_config.json "Your brief here"

# Step 3 — Verify init
ls output/my_project/
# Expected: features.json  init.sh  progress.md  .git/

# Step 4 — Check features
harness status output/my_project_a3f8c21b/harness_config.json

# Step 5 — Run (pick a coding-agent runner)
harness run output/my_project_a3f8c21b/harness_config.json                        # interactive prompt
harness run output/my_project_a3f8c21b/harness_config.json --runner subprocess    # Claude Code CLI
harness run output/my_project_a3f8c21b/harness_config.json --runner codex         # Codex CLI

# Step 6 — Monitor (separate terminal)
watch -n 10 "harness status output/my_project_a3f8c21b/harness_config.json"
```

---

## Resuming a Stopped Run

The harness is fully restartable. Just re-run — it skips init and planning, and picks up from the last PENDING/FAILING feature.

```bash
harness run output/my_project_a3f8c21b/harness_config.json --runner subprocess
```

---

## Project GitHub Sync

The Harness checkout ignores `output/`. Each generated or imported project is its own repo, and can push to its own GitHub remote during the workflow.

```bash
# New project, let gh create/check the GitHub repo
harness new --claude-code --github-repo owner/my_project

# Import a local repo into output/ and push the copy as a separate repo
harness import ../my_project --github-repo owner/my_project

# Use an existing remote URL
harness new --codex --git-remote git@github.com:owner/my_project.git
```

If sync fails, fix the remote/auth issue and resume the harness. Passing feature state is preserved.

---

## Runner Quick Reference

```bash
harness runners   # shows the three coding-agent rows

# Pick a coding agent (the runner)
harness run harness_config.json -r subprocess   # Claude Code CLI
harness run harness_config.json -r sdk          # Claude Code SDK
harness run harness_config.json -r codex        # Codex CLI

# Then pick the auth/billing mode by setting env vars BEFORE the run:
#
#   Mode 1 — Claude subscription      : (no env vars; you're signed into Claude Code)
#   Mode 2 — Claude API               : ANTHROPIC_API_KEY=sk-ant-...
#   Mode 3 — Codex subscription       : (no env vars; you're signed into Codex)
#   Mode 4 — OpenAI API               : OPENAI_API_KEY=sk-...
#   Mode 5 — Gemini API               : GEMINI_API_KEY=AIza...  (custom provider)
#   Mode 6 — OpenRouter API           : ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1
#                                       ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY
```

---

## Runner Rotation Policy

Use `harness setup` once to save named runner profiles and per-role fallback order.

```bash
harness setup \
  --profile claude:subprocess:sonnet \
  --profile codex:codex:gpt-5.2 \
  --profile claude-openrouter:subprocess:anthropic/claude-sonnet-4-6:openrouter \
  --profile-env claude-openrouter:ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1 \
  --profile-env 'claude-openrouter:ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY' \
  --planner-order codex,claude \
  --generator-order claude,codex,claude-openrouter \
  --evaluator-order codex,claude-openrouter
```

`harness new` and `harness import` copy the saved setup into each project config. On a usage cap, the active role retries with the next profile in its whitelist. If every profile for that role is capped, the normal pause/auto-resume behavior takes over.

---

## Incident Playbook

### INC-01: `FileNotFoundError: Features file not found`
**Cause:** `harness init` was never run, or `output_dir` in config is wrong  
**Fix:** `harness init output/my_project_a3f8c21b/harness_config.json "Your brief"`

---

### INC-02: Feature stuck in `IN_PROGRESS` after crash
**Cause:** Process crashed mid-feature without writing a result  
**Fix:**
```python
import json
from pathlib import Path

path = Path("output/my_project/features.json")
data = json.loads(path.read_text())
data["current_feature_id"] = None
for f in data["features"]:
    if f["status"] == "in_progress":
        f["status"] = "pending"
path.write_text(json.dumps(data, indent=2))
```

---

### INC-03: `subprocess` runner — `` `claude` binary not found ``
**Cause:** Claude Code CLI is not installed or not on PATH  
**Fix:**
```bash
# Install Claude Code from https://claude.ai/download
# Verify:
which claude
claude --version
```

---

### INC-04: `codex` runner — `` `codex` binary not found ``
**Cause:** OpenAI Codex CLI is not installed  
**Fix:**
```bash
npm install -g @openai/codex
# Verify:
which codex
codex --version
```

---

### INC-05: `sdk` runner — `claude_code_sdk not installed`
**Fix:**
```bash
pip install -e ".[sdk]"
# or: pip install claude-code-sdk
```

---

### INC-06: Coding-agent runner — required env var missing
**Cause:** You picked a non-subscription mode (Claude API / OpenAI API / OpenRouter / Gemini) but the env var Claude Code or Codex needs isn't set
**Fix:**
```bash
# Mode 2 — Claude API
export ANTHROPIC_API_KEY=sk-ant-...

# Mode 4 — OpenAI API
export OPENAI_API_KEY=sk-...

# Mode 6 — OpenRouter API (route Claude Code through OpenRouter)
export ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1
export ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY
```

The harness does NOT auto-export from `harness_config.json` — set the env vars in your shell.

---

### INC-07: Subscription rate limit hit mid-run
**Symptom:** Rich panel "Paused — rate limit" with a reset time; orchestrator exits cleanly, unless a runner profile fallback is available
**Cause:** Claude Code (Pro/Max) or Codex (Plus) usage cap reached
**Fix:** If role fallback profiles are configured, Harness rotates automatically. If no fallback remains and `auto_resume_on_rate_limit: true` (default), an OS-native one-shot resume is scheduled: launchd on macOS, systemd user timers on Linux, or Task Scheduler on Windows. Otherwise:
```bash
# wait until the reset time, then:
harness resume output/<project_dir>
```

---

### INC-07A: Project GitHub sync failed
**Cause:** Missing `gh`, unauthenticated GitHub CLI, bad repo name, or a remote push/auth problem
**Fix:**
```bash
gh auth login
gh auth status

# Or switch the project config to a remote URL you already control:
# "project_git_remote": "git@github.com:owner/my_project.git"
```

The workflow reports the sync failure separately from feature evaluation. After fixing auth or the remote, run `harness resume output/<project_dir>`.

---

### INC-08: Evaluator never returns structured output
**Cause:** Evaluator prompt too long or model refuses tool use  
**Note:** Evaluator uses Anthropic API in `api` orchestration mode, and the selected coding-agent runtime in `runner` mode.  
**Fix:**
1. Confirm the selected orchestration mode has the credentials/runtime it needs
2. Check `evaluator_model` is a capable model (`claude-opus-4-7`)
3. Reduce sprint contract criteria count (≤ 10 items)
4. Temporarily set `sprint_contract_enabled: false`

---

### INC-10: Context resets happening too frequently
**Cause:** `context_reset_threshold_tokens` is too low  
**Fix:**
```json
{
  "context_reset_threshold_tokens": 180000
}
```

---

### INC-11: Anthropic API 529 Overloaded
**Cause:** Anthropic API at capacity (affects Planner+Evaluator only when `orchestration_mode='api'`)
**Fix:** The Anthropic SDK retries automatically. If retries exhaust, wait 30–60s and re-run — progress is preserved.

---

### INC-12: Wrong conda environment active
**Symptom:** `ModuleNotFoundError: No module named 'harness'` or wrong Python  
**Fix:**
```bash
conda activate harness
python --version  # should be 3.12
harness --help
```

---

## Monitoring During a Long Run

```bash
# Progress summary
watch -n 30 "harness status output/my_project_a3f8c21b/harness_config.json"

# Markdown progress file
tail -f output/my_project/progress.md

# Git commits as features land
cd output/my_project && git log --oneline -f

# Full log with runner output
harness run output/my_project_a3f8c21b/harness_config.json -r subprocess 2>&1 | tee run.log
```
