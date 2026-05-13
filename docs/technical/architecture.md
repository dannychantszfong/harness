# Architecture — Claude Agent Harness

**Version:** 2.2
**Status:** Active
**Last updated:** 2026-05-07

---

## 1. System Overview

The Claude Agent Harness is a Python orchestration framework that drives a coding agent (Claude Code or Codex) through a defined lifecycle to build software projects. The harness itself is provider-agnostic: the same orchestration logic runs whether the underlying model is on a Claude subscription, the Anthropic API, OpenAI, Gemini via OpenRouter, etc.

This document describes the layers that make up the system, their responsibilities, the interfaces between them, and the design assumptions each layer holds.

---

## 2. Module Map (12 Layers)

```
                           ┌────────────────────────────┐
                           │  CLI                       │
                           │  cli.py                    │
                           └─────┬─────────────────┬────┘
                                 │                 │
                                 │  invokes        │  setup-only
                                 ▼                 ▼
┌──────────────────┐    ┌──────────────────┐   ┌──────────────────┐
│ Project Lifecycle│    │   Orchestration   │   │  Setup/Preflight │
│ import_repo.py   │◄──►│ orchestrator.py   │   │  preflight.py    │
│ project_git.py   │    │ (701 LOC, single  │   │  runner_profiles │
│                  │    │  coordinator)     │   │   (HarnessSetup) │
└────────┬─────────┘    └────────┬──────────┘   └────────┬─────────┘
         │                        │                       │
         │             ┌──────────┴───────────┐           │
         │             │                      │           │
         ▼             ▼                      ▼           │
┌──────────────────┐ ┌──────────────────┐ ┌────────────────────────┐
│   Persistence    │ │     Agents       │ │  Runners (3 transports)│
│ progress/        │ │ agents/          │ │  runners/              │
│   models.py      │ │   base.py        │ │   subprocess_runner    │
│   tracker.py     │ │   planner.py     │ │   sdk_runner           │
│                  │ │   initializer.py │ │   codex_runner         │
└──────────────────┘ │   generator.py   │ │   _rate_limit (shared) │
                     │   evaluator.py   │ └─────────┬──────────────┘
                     │   reviewer.py    │           │
                     └──────────────────┘           │
┌──────────────────────────────────────────────┐    │
│  Context / Session                           │    │
│  context/handoff.py · context/reset.py       │    │
│  session/opener.py                           │    │
└──────────────────────────────────────────────┘    │
                                                     │
┌──────────────────────────────────────────────┐    │
│  Platform                                    │    │
│  auto_resume.py — launchd | systemd |        │    │
│                    Task Scheduler            │    │
│  (init-script policy lives in config.py +    │◄───┘
│   initializer.py — see Layer 9)              │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│  Configuration                               │
│  config.py (HarnessConfig + RunnerProfile)   │
│  - durable project state                     │
│  - platform-aware properties                 │
│  - active_runner_env merge contract          │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│  UI                                          │
│  ui/spinner.py (QuietAnimator)               │
└──────────────────────────────────────────────┘
```

---

## 3. Layer Responsibilities

### Layer 1 — Configuration ([harness/config.py](../../harness/config.py))

Pure data layer (Pydantic). Two top-level models: `HarnessConfig` (per-project) and `RunnerProfile` (named runner+model+env recipe used by rotation chains).

Owns three platform-aware properties:

- `effective_init_script_type` → `bash | powershell | cmd`. Priority: explicit `init_script_type` field → suffix of explicit `init_script` filename → host platform default (PowerShell on Windows, bash elsewhere).
- `effective_init_script` → resolved filename, derived from type when not set.
- `startup_command_for_platform` → the exact shell command the evaluator/generator should run to start the generated app.

Owns the env-layer contract: see Layer 4.

### Layer 2 — Setup / Preflight ([harness/preflight.py](../../harness/preflight.py), [harness/runner_profiles.py](../../harness/runner_profiles.py))

Bootstrap-time concerns. Two distinct things:

- **`HarnessSetup`** model — the user's once-per-machine choice of named runner profiles + per-role rotation order. Persisted to `~/.harness/setup.json` (override via `HARNESS_SETUP_CONFIG` env var). Auto-applied to new projects.
- **`preflight.run_preflight()`** — verifies host readiness. `gh` is treated as core infrastructure (always required); coding-agent CLIs (`claude`, `codex`) are required only when a profile actually uses them.

`harness setup` invokes both: collect profiles → preflight → block save if any required tool is missing or `gh` is unauthenticated. Auto-install attempted only via `brew` (macOS) and `winget` (Windows), and only when the user passes `--auto-install`.

### Layer 3 — Runners ([harness/runners/](../../harness/runners/))

Three coding-agent transports, all implementing `CodeRunner.implement(prompt, cwd, timeout) → RunResult`:

| Runner | Backend | Notes |
|---|---|---|
| `subprocess` | `claude --print --dangerously-skip-permissions` | Stdout-only; rate-limit detection via `_rate_limit.parse_reset_time`. |
| `sdk` | `claude_code_sdk.query(...)` with `permission_mode="bypassPermissions"` | Streamed tool calls, structured token usage; uses `profile_env()` (mutates `os.environ`). |
| `codex` | `codex exec --dangerously-bypass-approvals-and-sandbox` | Same rate-limit hooks as subprocess. |

Direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not separate runners — they plug into one of these three via env vars (see Layer 4).

`_rate_limit.py` is the single source of truth for "is this failure a usage cap?" detection. All three runners use `looks_rate_limited()` (broad heuristic — sets `rate_limited=True` to trigger rotation) and `parse_reset_time()` (strict — populates `rate_limit_reset_at` when the CLI says e.g. "resets 9:30pm (Europe/London)").

### Layer 4 — Env Model

`HarnessConfig.active_runner_env` is the resolved env projected onto every runner call. It comes from two layers:

1. The user-authored base in `active_runner_env` itself — defaults that apply regardless of which profile the orchestrator picked.
2. The active `RunnerProfile.env` — per-profile overrides. Merged OVER the base when `runner_profiles.config_for_profile` activates a profile.

Profile keys win on collision; base keys survive when the profile doesn't override. Values like `"$OPENROUTER_API_KEY"` or `"${VAR}"` are resolved against the parent process env at call time.

The field used to be named `code_runner_env`. A pydantic alias keeps existing configs loading; serialization uses the new name.

`CodeRunner.subprocess_env()` is the safe path — builds a child-process env dict and passes it to `subprocess.run(env=…)`. Used by `subprocess_runner` and `codex_runner`. `CodeRunner.profile_env()` is a context manager that mutates the global `os.environ` for the duration of a call — used only by `sdk_runner` because the SDK has no `env=` parameter. **Single-process only**; this is the dominant blocker for any future parallel-feature execution.

### Layer 5 — Agents ([harness/agents/](../../harness/agents/))

Five agent classes, each with one responsibility:

- `PlannerAgent` — converts a brief into a confirmed product spec via interactive multi-turn alignment (`align_requirements`).
- `InitializerAgent` — runs once per project. Decomposes the spec into atomic features, writes the platform-aware init script, performs the initial git commit.
- `GeneratorAgent` — implements one feature per call. Always uses the runner.
- `EvaluatorAgent` — grades the generator's output against sprint contract criteria.
- `ReviewerAgent` — separate from `Evaluator`. Audits an entire project's docs/architecture/tests/CI/spec-drift and writes `REVIEW.md`. Used by `harness import` on review-ready repos.

`agents/base.py` owns the seam where rate-limit propagation happens. `_call_via_runner` raises `RunnerRateLimitedError` when the `RunResult` carries either `rate_limit_reset_at` or `rate_limited=True`.

### Layer 6 — Persistence ([harness/progress/](../../harness/progress/))

`Feature` and `ProjectProgress` Pydantic models + `ProgressTracker` for read/write of `features.json`. The on-disk format is the source of truth — restartable, human-inspectable, the basis for `harness resume`.

**Single-writer assumption.** No file lock today. Safe because feature execution is sequential. A future parallel-feature mode would need an advisory lock here (`fcntl` / `msvcrt`).

### Layer 7 — Context / Session ([harness/context/](../../harness/context/), [harness/session/](../../harness/session/))

Long-running state management. `ContextReset` decides when the conversation's token budget is exhausted; `HandoffDocument` serializes the next-session preamble; `SessionOpener` builds the standardized startup checklist that begins every session.

### Layer 8 — Project Lifecycle ([harness/import_repo.py](../../harness/import_repo.py), [harness/project_git.py](../../harness/project_git.py))

Two complementary concerns:

- **Import** — `detect_stage(project_dir)` is a pure function over file metadata. Classifies a directory into `HARNESS_PROJECT | HAS_FEATURES | HAS_CODE | EMPTY | REVIEW_READY`. Includes `assess_repo_spec_with_agent()` for cases where the heuristic isn't enough — the agent reads the repo and reports whether a usable spec exists (returns a `RepoSpecAssessment`).
- **Per-output-project Git** — every generated project under `output/` is its own independent git repository. `project_git.py` handles git init, remote configuration, and (optionally) GitHub repo creation via `gh`. The harness repo itself ignores `output/`.

`gh` is required infrastructure here (see Layer 2). Missing or unauthenticated `gh` raises `ProjectGitConfigurationError` — distinct from `ProjectGitSyncResult(ok=False)` which represents transient network/push issues. The orchestrator hard-fails on the former, warns on the latter.

### Layer 9 — Platform ([harness/auto_resume.py](../../harness/auto_resume.py))

OS-specific scheduling for one-shot post-cap resumes:

| Backend | OS | Status |
|---|---|---|
| launchd LaunchAgent | macOS | Primary, validated |
| systemd user service+timer | Linux | Experimental (mocked tests; not live-validated) |
| Task Scheduler + PowerShell wrapper | Windows | Experimental (mocked tests; not live-validated) |

`backend()` selects automatically based on `platform.system()` + `shutil.which()`. `is_supported()` returns False when no usable backend exists; the orchestrator falls back to printing a manual-resume hint.

**Other platform decisions** are owned closer to the data they parameterize:
- The init-script flavor lives in `HarnessConfig.effective_init_script_type` (Layer 1).
- The `chmod 0o755` policy lives in `InitializerAgent` (Layer 5) — only runs for `bash`.

Centralizing all platform branching in one module would create artificial indirection; the current placement keeps each decision next to the data it affects.

### Layer 10 — Orchestration ([harness/orchestrator.py](../../harness/orchestrator.py))

The single coordinator. Largest module by far (~700 LOC) because it ties together every other layer. Its concerns:

- Phase sequencing: Init → Plan → Feature loop (or just Review when `review_only=True`)
- The GAN loop: Generator ↔ Evaluator with sprint contracts
- Context reset triggering when the token budget is exhausted
- Live config reload at the natural seams (top of the feature loop, top of the GAN inner loop). Field allowlist in `_LIVE_RELOAD_FIELDS`. Identity fields (project_id, output_dir, code_runner, orchestration_mode) are intentionally pinned — changing them mid-run would invalidate the runner instance and progress files.
- Project-Git sync at init and after each passing feature
- Role-aware fallback rotation: `_with_role_fallback(role, call)` is the single seam. On `RunnerRateLimitedError` it advances to the next profile in that role's chain. Non-rate-limit failures propagate unchanged (this is intentional — see ADR-011).
- Auto-resume scheduling when rotation is exhausted and `reset_at` is known

### Layer 11 — UI ([harness/ui/spinner.py](../../harness/ui/spinner.py))

`QuietAnimator` — terminal animation while runners are silent. TTY-gated; opt-out via `HARNESS_NO_SPINNER=1`. Cross-platform (Rich handles Windows console).

### Layer 12 — CLI ([cli.py](../../cli.py))

Click commands: `new`, `resume`, `import`, `run`, `setup`, `runners`, `status`, `plan`, `init`, `animation-theme`. Translates flags into a `HarnessConfig`, hands off to `Orchestrator.run()`. The CLI is intentionally a thin layer — all behavior lives in the modules above.

---

## 4. Key Cross-Cutting Contracts

### Rate-limit handling

Three runners → one shared `_rate_limit.py` → `RunResult` with `rate_limited` (bool) and `rate_limit_reset_at` (Optional[datetime]) → `agents/base._call_via_runner` raises typed exception → `Orchestrator._with_role_fallback` rotates → `Orchestrator._handle_rate_limit` schedules.

Triggers: only `RunnerRateLimitedError`. Non-triggers: timeouts, generic non-zero exits, tool refusals, prompt-too-large, Anthropic 529 Overloaded. See [ADR-011](ADR.md#adr-011-profile-rotation-triggers-only-on-rate-limits).

### Setup vs runtime gh enforcement

`harness setup` blocks save when `gh` is missing/unauthenticated. `project_git.py` re-checks at runtime as defense in depth, raises `ProjectGitConfigurationError` on the same conditions, and the orchestrator hard-fails. Transient network/push errors stay non-fatal warnings.

### Platform-aware init script

| Configured | Filename | chmod | Default startup_command |
|---|---|---|---|
| `init_script_type="bash"` (or default on macOS/Linux) | `init.sh` | 0o755 | `bash init.sh` |
| `init_script_type="powershell"` (or default on Windows) | `init.ps1` | skipped | `powershell -ExecutionPolicy Bypass -File init.ps1` |
| `init_script_type="cmd"` | `init.bat` | skipped | `init.bat` |

Backward-compat: a config with explicit `init_script: "init.sh"` continues to resolve to bash regardless of host platform.

### Active env merge

Profile env merges over base env. Profile wins on collision. `$VAR` / `${VAR}` resolved at call time, not at config-load time, so secrets stay in the parent process env — not in `~/.harness/setup.json`.

---

## 5. Single-Process Assumptions

Documented here so they're easy to revisit when parallel execution lands:

| Layer | Shared mutable state | Impact under parallel features |
|---|---|---|
| Runners (`profile_env`) | `os.environ` | SDK runner races; subprocess+codex are safe (use `env=` arg) |
| Persistence (`tracker.save`) | `features.json` | No file lock — two writers will corrupt the file |
| Orchestrator (`_role_profile_index`, `_role_runner_cache`, `total_tokens`) | Per-instance attributes | Per-process safe; aggregation needed if features run in worker processes |
| Project git (`subprocess.run(["git", ...])`) | The single project repo's index/HEAD | Two parallel commits will race; needs per-feature worktrees or branch-per-feature |
| Auto-resume (`auto_resume.schedule`) | One launchd/systemd/Task Scheduler entry per project_id | Designed for a single coordinator; only one writer is correct |

None of these are blockers today. They become real once features can execute concurrently.

---

## 6. What's Next, Architecturally

Tracked as backlog items (not committed work):

- File-lock around `tracker.save()` (cross-platform via `fcntl.flock` / `msvcrt.locking`).
- SDK runner stops mutating `os.environ` (or marked single-process-only).
- Centralised platform info object if the platform-branch count grows beyond what's currently tolerable.
- Live validation of systemd + Task Scheduler backends on real Linux/Windows hosts.
- Real-host validation of Codex's behavior on Windows.
