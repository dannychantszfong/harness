# Runbook — Claude Agent Harness

**Audience:** Engineers running or debugging the harness  
**Version:** 2.0  
**Last updated:** 2026-10-11

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
# Expected: features.json  init.sh/init.ps1/init.bat  progress.md  .git/

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

The harness is fully restartable. `harness resume` works from any working
directory: the directory you pass is the project (a relative `output_dir`
written by an older Harness is corrected and saved).

```bash
harness resume output/my_project_a3f8c21b
```

Before any agent starts it prints passing / in-progress / pending /
exhausted counts, the feature it picks up and at which iteration, and
whether the latest handoff applies to that feature. Order of work:

1. an interrupted (`in_progress`) feature resumes first, with its last
   evaluator feedback;
2. then `pending` and `failing` features that still have iteration budget,
   by priority. The budget (`max_iterations_per_feature`) is per feature
   across runs;
3. exhausted features are skipped and listed (see INC-14).

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

### What does (and doesn't) trigger rotation

Runner rotation is intentionally conservative. Rotating providers makes
sense only when **the failure is recoverable by trying a different
provider**. For everything else we'd just be spending another
provider's quota chasing a problem that wasn't theirs.

**Triggers rotation:**

- `RunnerRateLimitedError` raised inside a role-fallback wrapper. The
  exception is created when a `RunResult` comes back with either
  `rate_limit_reset_at` set (subprocess parsed "You've hit your limit
  · resets …") OR `rate_limited=True` (broad heuristic match against
  the runner's stderr/exception text — covers SDK and Codex too).

**Does NOT trigger rotation:**

- Subprocess timeout (the timeout was the harness's choice, not the
  provider's; a different provider will likely time out the same way)
- Generic non-zero exit without rate-limit hints in stderr/stdout
- Tool refusal / content-policy failures
- Prompt-too-large errors
- Anthropic API 529 Overloaded — only relevant in
  `orchestration_mode='api'` for planner+evaluator. The Anthropic SDK
  retries automatically; rotation isn't applicable since the runner
  itself didn't fail.
- Any exception raised outside `_with_role_fallback(role, …)`. Code
  paths that call a runner directly (rare) won't rotate even on a
  genuine rate limit.

### Auto-resume vs rotation: which fires first?

Both are reactions to a `RunnerRateLimitedError`, but at different
scopes:

1. The orchestrator's role-fallback handler runs FIRST. If a next
   profile exists in the role's chain, we switch to it and retry the
   call — the user keeps making progress.
2. Auto-resume scheduling fires only when (a) every profile in the
   role's chain has been tried and exhausted in this run, AND (b)
   `reset_at` is known. Subscription rate-limit messages from the CLI
   carry a reset time; SDK-style 429s usually don't, so for those
   you'll see rotation but not scheduling.

---

## Incident Playbook

### INC-01: `FileNotFoundError: Features file not found`
**Cause:** `harness init` was never run, or `output_dir` in config is wrong  
**Fix:** `harness init output/my_project_a3f8c21b/harness_config.json "Your brief"`

---

### INC-02: Feature stuck in `IN_PROGRESS` after crash
**Cause:** Process crashed mid-feature without writing a result  
**Fix:** none needed since 1.0 — `harness resume <dir>` picks the
interrupted feature up first and continues its iteration count.

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
# or: pip install claude-code-sdk==0.0.25
```

The `sdk` runner is pinned to the frozen `claude-code-sdk` 0.0.25 (ADR-014);
preflight warns if another version is installed. See "SDK runner: migration
to claude-agent-sdk" below.

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

See [Auto-resume backends](#auto-resume-backends) for what each backend
does and how it was validated.

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
**Symptom:** "No usable grade from the evaluator" — Harness already re-asked
once (with the evaluator's previous answer) before counting the iteration.  
**Cause:** Evaluator prompt too long, model refuses tool use, or the
evaluator session failed  
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

### INC-13: "is not a valid Harness progress file" — the run stopped
**Cause:** `features.json` is not valid JSON or not a Harness progress
object (half-written by hand, merge conflict, edited by a tool).  
**What Harness did:** copied it to `features.json.corrupt-<timestamp>` and
stopped. It never re-decomposes or overwrites progress.  
**Fix:** repair the file, or restore the last committed copy:
```bash
git -C output/<project_dir> checkout -- features.json
harness resume output/<project_dir>
```

---

### INC-14: A feature "exhausted retries"
**Cause:** it used `max_iterations_per_feature` iterations without passing.  
**What Harness did:** committed the attempt to
`harness/attempt/<feature-id>/<n>`, wrote
`.harness/attempts/<feature-id>.md` (scores, feedback, weakest dimension,
last self-evaluation), and reset the working branch to the commit the
feature started from (ADR-015) — unless that would remove another
feature's recovery commit, in which case the code stays in place. Files the
attempt had git-ignored stay on disk, listed in `.git/info/exclude`. Other
features continued.  
**Fix:** read the notes; then either raise `max_iterations_per_feature` or
sharpen the feature's description in `features.json` while Harness is not
running, and `harness resume`. The retry gets the notes as lessons. To look
at the old code: `git -C output/<project_dir> diff main harness/attempt/<id>/1`.

---

### INC-15: "An agent modified features.json … restored Harness's copy"
**Cause:** a generator/evaluator session edited Harness's state file (or the
grading/permission settings in `harness_config.json` — edit those while
Harness is stopped).  
**What Harness did:** restored the snapshot taken before that session
(C-10). Nothing to fix; if it keeps happening, check the project's own
scripts don't write `features.json`.

---

### INC-17: "Stopped — evaluator unavailable"
**Cause:** the evaluator's runner failed twice in a row (expired sign-in,
missing binary on the evaluator's profile, crash).  
**What Harness did:** stopped without counting the iteration, so no
feature's budget was spent and nothing was reverted.  
**Fix:** fix the evaluator's runner, then `harness resume output/<project_dir>`.

---

### INC-16: Config fails to load — "holds a literal secret"
**Cause:** an API-key field or a secret-looking env value
(`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`), a `--api-key …`-style extra
argument, or a git remote with embedded credentials
(`https://<token>@github.com/…`) in `harness_config.json` or
`~/.harness/setup.json` contains the secret itself. These files are
committed / copied into project repos.  
**Fix:** replace the value with an environment reference, e.g.
`"ANTHROPIC_AUTH_TOKEN": "$OPENROUTER_API_KEY"`, and export the variable in
your shell.

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

## Auto-resume backends

When a runner reports a usage cap with a reset time and no fallback profile
is left, Harness schedules a one-shot `harness resume <absolute project
dir>` five minutes after the reset. Every wrapper writes to
`<project>/auto_resume.log`, removes its own schedule before resuming,
carries the `PATH` (and `HARNESS_SETUP_CONFIG`) from scheduling time so the
resumed run finds `claude`/`codex`, and never contains secrets — API-key env
vars must be available in your login environment.

| Backend | Status | Mechanism |
|---|---|---|
| launchd (macOS) | supported | user LaunchAgent + `/bin/sh` wrapper |
| Task Scheduler (Windows) | supported — validated 2026-10-11 | task registered from an XML definition (`schtasks /Create /XML`), PowerShell wrapper |
| systemd (Linux) | **experimental** — not yet run on a real host | user `.service` + `.timer` |

**Windows validation record (C-08, 2026-10-11).** Windows 10 Home 19045,
zh-CN locale (`yyyy/mm/dd` short dates), Windows PowerShell 5.1, Python
3.12.6, Harness installed from a wheel into a clean venv. A toy project in a
directory named `toy project 测试` (space + non-ASCII), written with a
relative `output_dir`, was scheduled 75 s ahead. The task fired at the
scheduled time, the wrapper ran `harness resume` with the absolute path,
Harness corrected the relative `output_dir`, resumed (no agent calls: all
features passing) and exited 0; the task deleted itself and no nested
`output/` was created. Problems found and fixed while preparing this:
`schtasks /SD` follows the system locale (replaced by an XML
`StartBoundary`), unquoted shell paths with spaces in `/TR`, and BOM-less
`.ps1` wrappers mangling non-ASCII paths under PowerShell 5.1 (now UTF-8
with BOM).

**Known pitfalls**

- systemd: `systemctl --user` needs a D-Bus user session; headless or SSH
  hosts usually need `loginctl enable-linger $USER`. Harness checks with
  `systemctl --user show-environment` and reports this instead of
  scheduling nothing. The timer uses `Persistent=true`, so a resume missed
  while the machine slept fires on wake.
- Task Scheduler: tasks use the interactive logon token, so they run only
  while you are logged on; `StartWhenAvailable` runs a missed task as soon
  as possible. Inspect with `schtasks /Query /TN HarnessResume-<id> /V`.
- launchd: `StartCalendarInterval` has minute resolution and no year; the
  wrapper unloads the job before resuming so it never fires twice.

To validate systemd on a Linux host, schedule a resume of a toy project a
minute or two ahead:

```bash
python - <<'PY'
from datetime import datetime, timedelta, timezone
from pathlib import Path
from harness import auto_resume
print(auto_resume.schedule(Path("output/toy"), "c08linux",
      datetime.now(timezone.utc) + timedelta(seconds=90), buffer_seconds=0))
PY
```

Then check `output/toy/auto_resume.log` and `systemctl --user list-timers`,
and record the result here and in ADR-012.

---

## SDK runner: migration to claude-agent-sdk

Planned for 1.1 (ADR-014). The steps, so they can be done in one change:

1. Depend on `claude-agent-sdk` instead of `claude-code-sdk==0.0.25`; import
   `query` and `ClaudeAgentOptions` from `claude_agent_sdk`.
2. Ask for Claude Code's system-prompt preset explicitly (the new SDK does
   not include it by default) and decide which settings sources to load
   (none are loaded by default) — otherwise the runner behaves differently
   from `claude -p`.
3. Keep the result handling: `ResultMessage.result`, `usage` (dict),
   `total_cost_usd`, `is_error` / `subtype`, and closing the stream on
   timeout.
4. Re-run `tests/test_runner_io.py` and one real feature end-to-end on the
   `sdk` runner before releasing.

---

## Monitoring During a Long Run

```bash
# Progress summary (tokens and cost included)
watch -n 30 "harness status output/my_project_a3f8c21b"

# Markdown progress file
tail -f output/my_project/progress.md

# Git commits as features land
cd output/my_project && git log --oneline -f

# Full log with runner output
harness resume output/my_project_a3f8c21b 2>&1 | tee run.log
```
