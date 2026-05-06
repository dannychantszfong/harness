# Runbook — Claude Agent Harness

**Audience:** Engineers running or debugging the harness  
**Version:** 2.0  
**Last updated:** 2026-05-03  

---

## Prerequisites

```bash
# 1. Create and activate the conda environment
conda create -n harness python=3.12 -y
conda activate harness

# 2. Install core harness
pip install -e .

# 3. Install provider extras (install what you need)
pip install -e ".[sdk]"           # Claude Code SDK runner
pip install -e ".[openai]"        # OpenAI + OpenRouter runners
pip install -e ".[gemini]"        # Google Gemini runner
pip install -e ".[all-providers]" # Everything

# 4. Set API keys (only needed for your chosen runners)
export ANTHROPIC_API_KEY=sk-ant-...    # required for Planner + Evaluator always
export OPENAI_API_KEY=sk-...           # openai runner
export GEMINI_API_KEY=AIza...          # gemini runner
export OPENROUTER_API_KEY=sk-or-...    # openrouter runner

# 5. Verify
harness --help
harness runners   # confirm table prints cleanly
```

---

## Standard Operating Procedure: New Project

```bash
conda activate harness

# Step 1 — Create config
cp examples/web_app.yaml my_project.yaml
# Edit: project_name, brief, output_dir, code_runner (optional)

# Step 2 — Initialize
harness init my_project.yaml "Your brief here"

# Step 3 — Verify init
ls output/my_project/
# Expected: features.json  init.sh  progress.md  .git/

# Step 4 — Check features
harness status my_project.yaml

# Step 5 — Run (pick a runner)
harness run my_project.yaml                        # interactive prompt
harness run my_project.yaml --runner subprocess    # Claude Code CLI
harness run my_project.yaml --runner openrouter    # OpenRouter

# Step 6 — Monitor (separate terminal)
watch -n 10 "harness status my_project.yaml"
```

---

## Resuming a Stopped Run

The harness is fully restartable. Just re-run — it skips init and planning, and picks up from the last PENDING/FAILING feature.

```bash
harness run my_project.yaml --runner subprocess
```

---

## Runner Quick Reference

```bash
harness runners   # shows the full table with requirements

# Agentic (uses subscription — no API billing)
harness run config.yaml -r subprocess   # needs: claude CLI installed
harness run config.yaml -r sdk          # needs: pip install -e ".[sdk]"
harness run config.yaml -r codex        # needs: codex CLI installed

# API (pay-per-token)
harness run config.yaml -r anthropic    # needs: ANTHROPIC_API_KEY
harness run config.yaml -r openai       # needs: OPENAI_API_KEY + pip install -e ".[openai]"
harness run config.yaml -r gemini       # needs: GEMINI_API_KEY + pip install -e ".[gemini]"
harness run config.yaml -r openrouter   # needs: OPENROUTER_API_KEY + pip install -e ".[openai]"
```

---

## Incident Playbook

### INC-01: `FileNotFoundError: Features file not found`
**Cause:** `harness init` was never run, or `output_dir` in config is wrong  
**Fix:** `harness init my_project.yaml "Your brief"`

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

### INC-06: `openai` / `openrouter` runner — `openai package not installed`
**Fix:**
```bash
pip install -e ".[openai]"
```

---

### INC-07: `gemini` runner — `google-generativeai not installed`
**Fix:**
```bash
pip install -e ".[gemini]"
```

---

### INC-08: API runner — `API key not set`
**Cause:** The required environment variable is missing  
**Fix:**
```bash
# Match the key to your runner:
export ANTHROPIC_API_KEY=sk-ant-...    # anthropic
export OPENAI_API_KEY=sk-...           # openai
export GEMINI_API_KEY=AIza...          # gemini
export OPENROUTER_API_KEY=sk-or-...    # openrouter
```
Or add it to your YAML config (use env vars for secrets in production):
```yaml
openai_api_key: "sk-..."
```

---

### INC-09: Evaluator never returns structured output
**Cause:** Evaluator prompt too long or model refuses tool use  
**Note:** Evaluator always uses Anthropic API regardless of runner choice.  
**Fix:**
1. Confirm `ANTHROPIC_API_KEY` is set
2. Check `evaluator_model` is a capable model (`claude-opus-4-7`)
3. Reduce sprint contract criteria count (≤ 10 items)
4. Temporarily set `sprint_contract_enabled: false`

---

### INC-10: Context resets happening too frequently
**Cause:** `context_reset_threshold_tokens` is too low  
**Fix:**
```yaml
context_reset_threshold_tokens: 180000  # max ~195k for Opus 4.7
```

---

### INC-11: Anthropic API 529 Overloaded
**Cause:** API is at capacity (affects Planner, Evaluator, and `anthropic` runner)  
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
watch -n 30 "harness status my_project.yaml"

# Markdown progress file
tail -f output/my_project/progress.md

# Git commits as features land
cd output/my_project && git log --oneline -f

# Full log with runner output
harness run my_project.yaml -r subprocess 2>&1 | tee run.log
```
