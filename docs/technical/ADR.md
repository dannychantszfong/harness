# Architecture Decision Records — Claude Agent Harness

**Last updated:** 2026-10-11  

---

## ADR-001: Context Resets over Compaction
*(unchanged)*

**Decision:** Use context resets with `HandoffDocument` rather than in-place compaction.  
**Rationale:** Resets eliminate context anxiety; compaction retains the model state that caused it.

---

## ADR-002: Separate Generator and Evaluator Agents
*(unchanged)*

**Decision:** Adversarial `EvaluatorAgent` with structured tool-use scoring.  
**Rationale:** Self-evaluation bias is intractable; separation creates a GAN-like quality loop.

---

## ADR-003: One Feature Per Session
*(unchanged)*

**Decision:** Each generator session implements exactly one feature.  
**Rationale:** Prevents context exhaustion and undocumented half-finished work.

---

## ADR-004: File-Based Agent Communication
*(unchanged)*

**Decision:** All shared state in files on disk (`features.json`, handoffs, git).  
**Rationale:** Survives crashes, human-inspectable, no external infrastructure.

---

## ADR-005: Pydantic for All Data Models
*(unchanged)*

**Decision:** Pydantic v2 for `Feature`, `ProjectProgress`, `EvaluationResult`, etc.  
**Rationale:** Built-in JSON round-trip, validation, IDE support.

---

## ADR-006: Sprint Contracts Before Implementation
*(unchanged)*

**Decision:** Generator proposes acceptance criteria before writing code.  
**Rationale:** Aligns generator and evaluator on "done" before work starts.

---

## ADR-007: Pluggable Runner Architecture

**Status:** Accepted  
**Date:** 2026-05-03  

### Context
The initial harness called the Anthropic API directly from `GeneratorAgent`. This created two problems:
1. Users with Claude subscriptions were billed at API rates instead of using their included subscription
2. Users on other providers (OpenAI, Gemini) couldn't use the harness at all

### Decision
Extract all execution logic into a `CodeRunner` abstract class with a single interface (`implement(prompt, cwd) → RunResult`). Implement runners only for **coding agents** (Claude Code CLI, Claude Code SDK, Codex CLI). Direct API providers are not standalone runners — they plug in as the *model* behind one of the agents.

### Rationale
- Single interface keeps the orchestrator completely provider-agnostic
- Coding agents (Claude Code, Codex) provide the harness's actual value: tool use, file I/O, shell, git workflow. A single-turn API call cannot match this.
- Two agents × six auth modes (subscription / Anthropic API / OpenAI API / Gemini / OpenRouter / etc.) cover the realistic deployment matrix without bloating the runner count.
- Runner swap requires only a config change or CLI flag — no code changes

### Consequences
- `GeneratorAgent` constructor now requires a `CodeRunner` instance
- `Orchestrator` resolves runner at startup and injects it
- Sprint contract negotiation remains on the Anthropic API (it's a structured tool call, not an agentic task — no file I/O needed)
- Token/cost data is `None` for agentic transports that don't surface per-call usage (the SDK transport does expose tokens via streaming)

---

## ADR-008: Two Coding Agents, Six Modes (revised 2026-05-06)

**Status:** Accepted (revises 2026-05-03)
**Date:** 2026-05-06

### Context
The earlier design exposed direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) as their own first-class runners. In practice, that gave users an inferior single-turn experience for the same money — no file I/O, no tool use, no agentic behavior. The harness's value comes from running on top of a real coding agent.

### Decision
The harness recognizes exactly **two coding agents** and **six modes**:

| # | Mode | Agent | Auth source |
|---|---|---|---|
| 1 | Claude subscription | Claude Code (subprocess / sdk) | Pro/Max plan |
| 2 | Claude API | Claude Code | `ANTHROPIC_API_KEY` |
| 3 | Codex subscription | Codex | OpenAI Plus plan |
| 4 | OpenAI API | Codex | `OPENAI_API_KEY` |
| 5 | Gemini API | Codex (custom provider) or Claude Code (via OpenRouter) | `GEMINI_API_KEY` |
| 6 | OpenRouter API | Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY` |

The four standalone API runners are removed. API keys flow into the three coding-agent runners via env vars.

### Rationale
- Running `claude --print` (or the SDK) is the same agent regardless of how the model is paid for — picking a billing source is orthogonal to picking the agent
- Removing the four runners eliminates ~600 lines of pay-per-token API code that duplicated what Claude Code / Codex already do better
- Users keep every previously-supported provider; they just access it through a tool-using agent shell instead of as a single-turn text generator

### Consequences
- `harness runners` and the interactive picker show only the three coding-agent options
- The four named CLI flags (`--anthropic-api`, `--openai-api`, `--gemini`, `--openrouter`) are removed; users select the agent (`--claude-code` / `--claude-sdk` / `--codex`) and set the matching env var
- `RunnerType.api_based()` returns `[]` (kept as a method so callers don't break)
- Existing project configs that reference removed runner values must migrate — `harness import` / `harness resume` surface a clear error

---

## ADR-009: Runner Selection via Three-Tier Priority

**Status:** Accepted  
**Date:** 2026-05-03  

### Context
Runner selection needs to be flexible for both first-time users (want guidance) and power users (want to skip prompts).

### Decision
Resolve runner with this priority:
1. `--runner` / `-r` CLI flag — highest; useful for scripting and one-off overrides
2. `"code_runner"` in `harness_config.json` — for projects that always use the same runner
3. Interactive prompt — shown only when neither of the above is set

### Rationale
- CLI flag enables scripting (`harness run harness_config.json -r subprocess`) without touching config
- Config field enables "set it and forget it" per-project defaults
- Interactive prompt with a formatted table ensures new users understand their options
- No hidden defaults — if the user hasn't chosen, they're asked

### Consequences
- `Orchestrator.__init__` accepts an optional `runner_type` argument
- CLI `run` command has a `--runner` option
- `HarnessConfig` has an optional `code_runner` string field

---

## ADR-010: Orchestration Mode Controls Agent Routing

**Status:** Superseded by runner/api orchestration modes  
**Date:** 2026-05-03  

### Context
Should all agents (Planner, Evaluator, Initializer) be routed through the chosen runner?

### Decision
`GeneratorAgent.implement_feature()` always uses the selected `CodeRunner`. `InitializerAgent`, `PlannerAgent`, and `EvaluatorAgent` follow `orchestration_mode`:

- `runner`: use the selected coding-agent runtime too
- `api`: call the Anthropic API directly

### Rationale
- Runner mode lets Claude Code/Codex act as the full coding-agent frame without requiring API keys
- API mode remains useful when precise Anthropic API control is preferred for planning/evaluation
- The evaluator has runner-mode XML parsing as a fallback when API tool use is not available

### Consequences
- Subscription-first runs can be fully API-key-free
- API orchestration still requires `ANTHROPIC_API_KEY`
- Model selection for agentic runtimes lives in `code_runner_model`

---

## ADR-011: Profile Rotation Triggers Only on Rate Limits

**Status:** Accepted
**Date:** 2026-05-07

### Context
With `runner_profiles` + per-role `*_runner_order` chains, the orchestrator
can route a role's call to a fallback profile when the active runner
fails. The question is: which failures should rotate?

### Decision
**Rotate only on `RunnerRateLimitedError`.** Concretely:

- A `RunResult` with `rate_limit_reset_at` set (CLI parse hit) → rotate.
- A `RunResult` with `rate_limited=True` (broad heuristic match against
  stderr/exception text — covers SDK and Codex too) → rotate.
- Anything else (timeouts, non-zero exits without rate-limit hints,
  tool refusals, content-policy failures, prompt-too-large) → propagate
  as a normal `RuntimeError` and DO NOT rotate.

The contract is enforced at one seam: `Orchestrator._with_role_fallback`.
Any direct runner call that bypasses this seam will NOT rotate.

### Rationale
Rotating providers makes sense only when the failure is *recoverable
by trying a different provider*. For a timeout or a content-policy
hit, the next provider will likely fail the same way, just slower and
more expensive. Conservative is correct.

The SDK runner historically didn't populate `rate_limit_reset_at`
because the SDK doesn't surface a reset time. Without the
`rate_limited=True` heuristic, SDK rate-limit hits would never trigger
rotation. The dual signal closes that gap.

### Consequences
- Auto-resume scheduling fires only when the active provider reported
  a parseable reset time. SDK/Codex rate-limit hits trigger rotation
  but not scheduling — the user keeps making progress on the next
  profile, no schedule is needed.
- Adding new failure classes to "should rotate" requires editing the
  rate-limit detection in `harness/runners/_rate_limit.py`, not the
  orchestrator. Centralized.
- A future `prompt_too_large` rotation policy (e.g. fall back to a
  larger-context model) would need a separate signal and a separate
  rotation rule — out of scope for now.

---

## ADR-012: Cross-Platform Scheduling Backends Are Experimental

**Status:** Accepted
**Date:** 2026-05-07

### Context
`harness/auto_resume.py` ships three backends to schedule a one-shot
`harness resume` after a subscription cap reset: launchd (macOS),
systemd user timer (Linux), Task Scheduler (Windows). Development
happens on macOS only. Linux and Windows backends are unit-tested
with mocked subprocess calls but have not been run against real hosts.

### Decision
- Treat launchd as primary/supported; systemd and Task Scheduler as
  *experimental* — clearly labelled in the runbook, never silently
  promoted to "supported" until live-validated.
- Continue shipping all three so users on Linux/Windows can try them
  and report failures.
- When a backend's underlying tool is missing (`launchctl`/`systemctl`/
  `schtasks`), `auto_resume.is_supported()` returns False and the
  orchestrator prints a manual-resume hint instead of attempting to
  schedule.

### Rationale
The auto-resume work is a quality-of-life feature, not a correctness
requirement. Shipping experimental backends behind the same flag and
defaulting to manual-resume when they fail keeps users unblocked
without overstating support.

### Consequences
- Real Linux/Windows validation will surface specific bugs
  (D-Bus session, schtasks quoting, account permissions, …) — fix
  those when we have hosts to test on.
- The runbook lists the known pitfalls per backend so users have a
  starting point if a backend silently does nothing.

**Update 2026-10-11 (Harness 1.0, C-08):** the Task Scheduler backend was
validated on a real Windows 10 host: a one-shot resume fired at the
scheduled time, ran `harness resume` on a project whose path contains a
space and non-ASCII characters, corrected a relative `output_dir`, exited 0
and deleted its task. It is now registered from an XML task definition
(locale-independent start time, `StartWhenAvailable`) instead of
`/SC ONCE /SD <locale date>`, so it is **supported**. The systemd backend
gained `Persistent=true` and a user-session check but has still not run on
a real Linux host — it stays **experimental**. launchd remains supported.

---

## ADR-013: Functionality Floor on Top of the Weighted Score

**Status:** Accepted (decision D-01)
**Date:** 2026-10-11

### Context
The pass decision was `overall ≥ evaluator_pass_score` only. With the
default weights a feature scoring 9/9/9 on design, originality and craft
but 3 on functionality averages 8.1 and passes. ADR-002 defines
functionality as "does it work correctly end-to-end"; a weighted average
cannot express a hard requirement. The model's self-reported overall score
was also trusted, and the configured weights were never used.

### Decision
- Harness always computes `overall = Σ score_i × weight_i` from the four
  sub-scores and the configured weights; the model's reported overall is
  stored as `reported_overall` for diagnostics only.
- A feature passes only if `overall ≥ evaluator_pass_score` **and**
  `functionality ≥ evaluator_functionality_floor` (default 6.0).
- Weights must sum to 1.0 (validated on load) and are rendered into the
  evaluator prompts. Scores are clamped to 1–10; unreadable output is an
  explicit parse failure, re-asked once, never a silent 5.0.

### Rationale
The GAN gate is only as strong as its verdict. A refinement of the
existing scoring — not a new scheme — closes the "beautiful but broken"
hole without changing anyone's weights.

### Consequences
- Some features that used to pass now iterate further.
- `EvaluationResult` gains `parse_failed`, `reported_overall` and
  `failure_reason`; old `features.json` files still load.

---

## ADR-014: Keep the Frozen claude-code-sdk for 1.0

**Status:** Accepted (decision D-02)
**Date:** 2026-10-11

### Context
The `sdk` runner uses `claude-code-sdk`, frozen at 0.0.25 and superseded
by `claude-agent-sdk`. The runner read the result message wrongly (usage
is a dict, the cost field is `total_cost_usd`, `is_error` and `result`
were ignored). Migrating changes more than the import name:
`ClaudeCodeOptions` → `ClaudeAgentOptions`, Claude Code's system-prompt
preset is no longer included unless requested, and settings sources are
not loaded by default — all of which could change how the runner behaves.

### Decision
Fix the field handling in place, pin `claude-code-sdk==0.0.25`, and note
in preflight that the runner uses the frozen SDK with migration planned
for 1.1 (preflight warns when another version is installed). The
`subprocess` runner calls the `claude` binary directly and is unaffected.

### Rationale
0.0.25 spawns the same `claude` CLI and still works; a migration with
behavioural side effects does not belong in a completion release.

### Consequences
- Re-evaluate if the pinned SDK breaks against a newer CLI.
- The migration path is in the runbook ("SDK runner: migration to
  claude-agent-sdk").

---

## ADR-015: Harness-Owned Recovery Points; Revert Exhausted Attempts

**Status:** Accepted (decision D-03)
**Date:** 2026-10-11

### Context
PRD P1 and US-08 require a git commit per passing feature as a recovery
point. Harness relied on the generator obeying a prompt line. When a
feature exhausted its iteration budget, its half-built code stayed in the
working tree and the next feature started on a broken baseline.

### Decision
- After a feature passes, Harness commits the working tree itself
  (`feat(<id>): <name> — score <x>`) and records `Feature.commit_sha`;
  `Feature.base_commit_sha` records HEAD when the feature started.
- When a feature exhausts its budget:
  1. the attempt is committed and preserved on
     `harness/attempt/<feature-id>/<n>` — never deleted;
  2. `.harness/attempts/<feature-id>.md` records every evaluation, the
     failure pattern, the generator's last self-evaluation and the branch;
  3. the working branch is `git reset --hard` to `base_commit_sha`, with
     Harness state files snapshotted before and restored after;
  4. any later attempt at the feature gets the attempts file as "lessons
     from previous attempts";
  5. the final panel, `progress.md` and `harness status` list exhausted
     features with their notes and branch.
- Git operations run only when the project directory is the root of its
  own repository — never on a parent repo.
- The base is kept only when a feature continues directly after an
  interruption; any other restart records HEAD as the new base. A reset
  that would remove another feature's recovery commit is skipped (the
  attempt's code stays in place, and is still preserved on its branch).
- Untracked files the attempt had git-ignored are not deleted; paths the
  base no longer ignores go to `.git/info/exclude`, and the follow-up
  commit stages only Harness's own files, so nothing the attempt left
  behind (e.g. `.env`) is committed or pushed.
- A feature's base is only recorded after everything on disk apart from
  Harness's state files is committed (`chore(harness): snapshot before
  <id>`, or `feat(<ids>): passing work committed by Harness …` when a
  passing feature's own commit had failed — a rejecting pre-commit hook, or
  Harness killed before committing). Those features get that commit as
  their `commit_sha`. If the commit fails, no base is recorded and the
  attempt is never reset; nor is any attempt reset while a passing feature
  has no `commit_sha`. Harness does not bypass hooks.
- Before any `git add -A`, directories with their own `.git` (scaffolders
  that run `git init`) are made plain directories: the nested `.git` moves
  to `<git-dir>/harness/nested-repos/`. Otherwise git records a gitlink with
  no `.gitmodules` (clones break) that `reset --hard` cannot remove.
  Submodules declared in `.gitmodules` are left alone.

### Rationale
"Revert to the last stable state, but keep the failed attempt for later
reference": every feature starts from a passing baseline, and no work is
lost.

### Consequences
- The iteration budget is per feature across runs; raising
  `max_iterations_per_feature` re-queues exhausted features.
- Without git (or outside a repo root) Harness still writes the notes but
  cannot commit, branch or revert, and says so.

---

## ADR-016: Bounded Tool Loop for the API-Mode Evaluator

**Status:** Accepted (decision D-04, option a)
**Date:** 2026-10-11

### Context
In `orchestration_mode: api` the evaluator was a single
`messages.create` with only `submit_evaluation`. Its prompt asked it to run
the startup command and navigate the app, which it could not do, so it
graded the generator's self-report — the bias ADR-002 set out to remove.

### Decision
`BaseAgent._call_with_tools` runs a bounded tool loop: `read_file`,
`list_files`, `search`, and `run_command` restricted to an allow-list (the
startup command, `test_command`, `git diff|log|status|show`), confined to
the project directory, without a shell, with the process tree killed on
timeout (`api_command_timeout_seconds`). After `api_tool_rounds` rounds
(default 20) the final tool is forced. The allow-list is the permission
boundary; which tools to use, and in what order, is the evaluator's own
judgement. The api-mode initializer reuses the loop to read an imported
repo.

The runner-mode counterpart is role-appropriate permissions (C-06):
planner, evaluator and sprint-contract sessions are read-only.

### Rationale
Evaluation must be based on the project, not on the generator's account
of it, in both orchestration modes.

### Consequences
- API-mode evaluations cost more tokens (bounded by `api_tool_rounds`).
- Commands that need a shell (pipes, `&&`) are refused; put them in the
  startup script or the test command.
- git options that write files or reach outside the repository
  (`--output`, `--no-index`, `-O<file>`, `--ext-diff`, …) are refused, and
  diff/log/show always run with `--no-ext-diff --no-textconv`. Runner-mode
  read-only sessions allow only exact git invocations for the same reason.
  Short-option bundles (`-pO<file>`) and abbreviations git would expand to
  a refused option (`--outp=`, `--textc`, `--no-i`) are refused as well.
- `search` skips files whose real path is outside the project (symlinks),
  like `read_file` and `list_files`.

---

## ADR-017: Agent Sessions Die With Harness; One Harness per Project

**Status:** Accepted (review pass 2, R-02/R-03)
**Date:** 2026-10-11

### Context
Agents run with full write access. Phase A started each session in its own
process group (so a timeout could kill the whole tree behind a `.cmd` or
shell shim), which also meant the session no longer received the
terminal's signals: when Harness was killed or its terminal closed, the
agent kept editing the project, and `harness resume` started a second one.
Separately, nothing stopped two Harness processes in one project — a
manual resume followed by the scheduled auto-resume job ran two
generators and two writers of `features.json`.

### Decision
- `process_guard.install()` (CLI commands that run agents): SIGTERM and
  SIGHUP raise `SystemExit`, so `run_process` kills the session's process
  group on the way out, as for Ctrl+C. Ignored signals stay ignored
  (`nohup`). On Linux sessions also get `PR_SET_PDEATHSIG=SIGKILL`. On
  Windows Harness joins a Job Object with `KILL_ON_JOB_CLOSE`: every child
  is in the job from creation, and Windows kills them when Harness exits
  in any way.
- `run_lock`: `run`, `resume`, `import`, `init`, `plan` (and
  `Orchestrator.run`) take an OS file lock on `<project>/.harness/run.lock`
  (`flock`; a byte-range lock on Windows) before writing anything. The file
  records pid, host, start time and command for the refusal message.
- A run that is not the scheduled job itself (`HARNESS_AUTO_RESUME_JOB`)
  cancels a pending auto-resume for the project.

### Rationale
An OS lock disappears with its process, so a crash never leaves a stale
lock and no pid-reuse heuristics are needed. A job object is the only
Windows mechanism that also covers a hard kill of Harness.

### Consequences
- Processes an agent leaves running (a dev server) end with Harness.
- Not covered: a SIGKILLed Harness on macOS, and grandchildren of a
  session on Linux after SIGKILL; the `sdk` runner's cleanup is the SDK's.
- The lock file is not a state file: it is never snapshotted, restored or
  committed (it is in the project's `.gitignore`).
