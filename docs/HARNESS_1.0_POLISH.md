# Harness 1.0 — Completion & Polish Plan

**Status:** Implemented 2026-10-11 on branch `harness-1.0` (tags `v1.0-a` … `v1.0-e`) — see §9 for the record
**Baseline:** `harness-main` snapshot reviewed 2026-10-11 — 185 tests passing (2 skipped) on Python 3.12
**Scope:** Finish the *original* design as recorded in `docs/product/PRD.md` (v2.0), `docs/product/user_stories.md` and `docs/technical/ADR.md` (ADR-001 … ADR-012). This is a completion plan, not a redesign.

---

## 0. How to use this document

- **Audience:** the project owner and the coding agent that executes the work.
- **Thesis is frozen.** Harness 1.0 is: *one-paragraph brief → planner spec → ordered feature list → per-feature generator/evaluator (GAN) loop → file-based state → autonomous until all features pass → subscription-first, everything routed through Claude Code / Codex.* Every change below must serve that thesis. Anything from the "Harness 2.0" discussions (Jira-driven backlog, orchestrator/coder roles, human escalation tickets, dashboards, multi-runtime connector layer) is **out of scope** — see §8.
- **IDs.** `P-xx` = defect to fix. `C-xx` = planned-but-unfinished capability to complete. `D-xx` = owner decision; all four (D-01 … D-04) were decided on 2026-10-11 and are recorded inline where they apply.
- **Effort.** S ≈ under 2 h, M ≈ half a day, L ≈ 1–2 days (for a coding agent, including tests).
- **Work order** is in §6. Phase A must come first: the Harness 2.0 fork is seeded from the infrastructure layer, so those fixes are made once and inherited.

---

## 1. What Harness 1.0 is

A CLI (`harness`) that takes a short brief, has a planner expand it into a spec (with interactive alignment), has an initializer decompose it into 50–200 atomic features plus a startup script, then runs each feature through a generator ↔ evaluator loop until a weighted score clears a threshold. All agent work runs through a *coding-agent runtime* (Claude Code CLI, Claude Code SDK, or Codex CLI) so a subscription is the only requirement. State lives in files (`features.json`, `progress.md`, `spec.md`, `handoff_*.json`) so a run can be killed and resumed. Subscription usage caps are detected and a one-shot OS job resumes the run after the reset time. Each output project is its own git repository and can be pushed to its own GitHub repo.

Layers (≈ 5,700 LOC source, 4,500 LOC tests):

| Layer | Files | Role |
|---|---|---|
| CLI | `cli.py` | `setup · new · run · resume · import · status · init · plan · runners · animation-theme` |
| Config | `harness/config.py`, `harness/runner_profiles.py` | per-project `harness_config.json`; global `~/.harness/setup.json`; per-role runner fallback chains |
| Orchestration | `harness/orchestrator.py` | Initialize → Plan → Feature loop; rate-limit rotation; live config reload at seams |
| Agents | `harness/agents/*` | Planner, Initializer, Generator, Evaluator, Reviewer |
| Runners | `harness/runners/*` | `CodeRunner` ABC; subprocess / sdk / codex; rate-limit parsing; env layering |
| State | `harness/progress/*`, `harness/context/*`, `harness/session/*` | features.json, progress.md, handoff documents, session opener |
| Ops | `harness/auto_resume.py`, `harness/project_git.py`, `harness/preflight.py`, `harness/import_repo.py`, `harness/ui/*` | scheduled resume (launchd/systemd/schtasks), GitHub sync, tool checks, repo import, terminal animation |

---

## 2. Inventory — what exists today

Status key: ✅ done and working · 🟡 implemented but partial / inert in default config · ❌ not started

### 2.1 PRD goals

| PRD item | Status | Notes |
|---|---|---|
| **P0** Build a project from a one-paragraph brief | 🟡 | Flow exists (`new` → planner → initializer → loop). Blocked in practice by P-01/P-02 on any interruption. |
| **P0** Arbitrary-length projects across sessions and context resets | 🟡 | Resume skips interrupted features (P-02); context reset never fires (C-01). |
| **P0** Scored GAN-style output | 🟡 | Loop exists; pass decision trusts the model's self-reported score and ignores configured weights (P-05, P-06). |
| **P0** Feature-level progress persisted | ✅ | `features.json` + `progress.md`. |
| **P0** Claude Code and Codex as coding agents | ✅ | Three runners. |
| **P1** Interactive runner selection | ✅ | |
| **P1** Runner via config or CLI flag | ✅ | |
| **P1** Sprint contract negotiation | 🟡 | API mode only; runner mode returns a stub contract (C-03). |
| **P1** Git commit per passing feature as a recovery point | ❌ | Harness never commits; it relies on the generator obeying a prompt line (C-02). |
| **P1** Optional push to the project's own GitHub repo | ✅ | `project_git.py`. |
| **P1** `harness setup` runner rotation policy | ✅ | |
| **P1** Six billing/auth modes | ✅ | Env-driven; Mode 5 (Gemini via Codex custom provider) is documented but unverified. |
| **P2** Cost tracking per session | ❌ | SDK fields read wrongly → always 0; subprocess returns None although `--output-format json` provides usage (C-04). `ProjectProgress.total_tokens_used/total_cost_usd` exist and are never written. |
| **P2** Playwright-based verification in the evaluator | ❌ | Only mentioned in the evaluator system prompt (C-07). |
| **P2** `harness runners` | ✅ | |

### 2.2 Feature acceptance criteria that currently fail

| Criterion (source) | Why it fails today |
|---|---|
| F-03 "each runner validates its prerequisites … clear error" | Preflight is skipped entirely when `runner_profiles` is set (P-25). |
| F-05 "`sdk` runner exposes per-call token usage" | Reads `usage` as an object; it is a dict → 0 (P-11). |
| F-06 "rate-limit hit … auto-resumed via the OS scheduler" | Scheduler job runs `cd <project> && harness resume <project>`; relative `output_dir` then resolves to a non-existent nested path and the initializer restarts from scratch (P-01). |
| F-08 / ADR-001 context reset with handoff | `should_reset()` depends on token counts that are always None/0 (C-01). |
| US-08 "every passing feature committed to git immediately" | Not done by Harness (C-02). |
| Success metric "Context reset recovery rate 100% — no features lost" | Features *are* lost on any interruption (P-02). |
| Success metric "Runner error messages actionable" | A missing `cwd` is reported as "`claude` binary not found" (P-12). |
| README "Every runner validates itself before the first feature runs" | False with profiles (P-25). |
| README "score ≥ threshold → git commit" | Harness does not commit (C-02). |
| README "99+ tests" / project-structure listing | Stale (185 tests, 15 files) (P-31). |

### 2.3 Items the owner's own roadmap (`development_path.txt`) listed

All five are implemented: progress animation ✅, import existing repo ✅, skip-permissions ✅, scheduled resume after usage cap ✅ (blocked by P-01), per-project GitHub repos ✅.

---

## 3. Defects to fix

### A. State and resume

**P-01 — Relative `output_dir` breaks `resume` and auto-resume.** `cli.py:758` stores `./output/<slug>_<id>`; `resume` (`cli.py:873-882`) never reconciles `config.output_dir` with the directory it was given; the scheduler wrappers (`auto_resume.py:195-196`, `:295-296`) `cd` into the project first. Result: a fresh nested `output/` and a second initializer run.
*Fix:* persist `output_dir` as an absolute, resolved path in `save_file`; in `resume`, override `config.output_dir` with the resolved `project_dir` argument and warn if they differ. Regression test: config with relative `output_dir`, run `resume` with `cwd` = project dir.
*Effort:* S.

**P-02 — Interrupted features are never retried.** `next_pending_feature()` (`progress/models.py:94-96`) returns only `PENDING`; `tracker.mark_in_progress` / `record_evaluation` set `IN_PROGRESS` / `FAILING` and nothing ever resets them.
*Fix:* at feature-loop start (and in `resume`), features in `IN_PROGRESS` are re-queued; `FAILING` features are re-queued while `iteration_count < max_iterations_per_feature`; features that exhausted their budget are reported explicitly in the final panel ("3 features exhausted retries: …") rather than silently counted as not passing. Keep `FAILING` as the on-disk status; add an `exhausted` flag or derive it from `iteration_count`.
*Effort:* S–M. Related: C-09.

**P-03 — A corrupted `features.json` is silently re-decomposed.** `initializer._load_or_promote_existing` (`initializer.py:267-279`) swallows the validation error and falls through to a fresh decomposition, overwriting progress.
*Fix:* if the file exists and is neither a valid `ProjectProgress` nor a bare list, back it up to `features.json.corrupt-<timestamp>` and **abort with a clear message**; never overwrite.
*Effort:* S.

**P-04 — Duplicate / malformed feature entries are not validated.** Runner-written `features.json` entries missing `id`/`name`/`description` raise `KeyError`; duplicate ids make `get_feature` return the wrong feature.
*Fix:* validate on load; de-duplicate ids deterministically (`f1`, `f1-2`, …) with a warning.
*Effort:* S.

### B. Evaluation integrity

**P-05 — The model's self-reported `overall_score` is trusted.** `evaluator.py:71` (`or` fallback never triggers because `_float` returns 5.0) and `:218` (`computed_score` is dead code because `overall_score` is `required` in the tool schema).
*Fix:* Harness always computes `overall = Σ score_i × weight_i` from the four sub-scores and the configured weights; the model-reported overall is logged for diagnostics only.
*Effort:* S.

**P-06 — Configured weights are never used and the prompt hardcodes 0.30/0.30/0.25/0.15** (`evaluator.py:23-26, :53, :98`; `EvaluatorWeights.validate_sum` in `config.py:22` is never called).
*Fix:* render weights from config into both prompts; call `validate_sum()` on config load; `_print_eval_summary` already uses config weights.
*Effort:* S.

**P-07 — Parse failure is indistinguishable from a real 5.0.** Missing XML → every dimension 5.0, feedback "No feedback provided.", and the generator iterates blindly.
*Fix:* return an explicit `parse_failed=True` result; the orchestrator re-asks the evaluator once with a "you must output the XML block" reminder before counting the iteration; log the raw output tail.
*Effort:* S.

**P-08 — No validation of score ranges or numeric shape.** `95` passes; `..` crashes `float()`.
*Fix:* clamp to 1–10 (log when clamped); tighten the regex to `\d+(?:\.\d+)?`; treat anything unparseable as P-07.
*Effort:* S.

**P-09 — A feature that does not work can pass.** With defaults, 9/9/9/**3** = 8.1 ≥ 8.0. ADR-002 defines Functionality as "does it work correctly end-to-end"; a weighted average alone cannot express a hard requirement.
*Fix (D-01 — DECIDED: yes):* add `evaluator_functionality_floor` (default 6.0) — a feature passes only if `overall ≥ pass_score` **and** `functionality ≥ floor`. This is a refinement of the existing scoring, not a new scheme; keep weights as they are. Record as ADR-013.
*Effort:* S.

### C. Runners

**P-10 — The prompt is passed as a single argv element** (`subprocess_runner.py:82`, `codex_runner.py:92`). Linux rejects a single argument over 128 KiB (`E2BIG`), which the planner's alignment loop reaches after a few rounds because it concatenates the whole history (`planner.py:81-85`); the resulting `OSError` is not caught; the prompt is visible in `ps`; a prompt beginning with `-` is parsed as a flag.
*Fix:* pipe the prompt via stdin (`claude -p` reads stdin when no positional prompt is given; `codex exec -` — verify the exact flag for the installed version); catch `OSError`.
*Effort:* S.

**P-11 — SDK runner reads the result message wrongly** (`sdk_runner.py:123-139`). `ResultMessage.usage` is a `dict` (→ `getattr` returns 0), the cost field is `total_cost_usd` not `cost_usd`, `is_error` is ignored (an `error_max_turns` session returns `success=True`), and `result` (the final message) is ignored in favour of concatenating every intermediate assistant text.
*Fix:* read `usage["input_tokens"]` / `usage["output_tokens"]`, `total_cost_usd`, honour `is_error`/`subtype`, use `result` as `output` (keep the concatenation only as a fallback when `result` is None).
*Decision D-02 — DECIDED: do not migrate in 1.0.* The SDK is used only by the `sdk` runner; the `subprocess` runner (the "terminal" way of using Claude Code) calls the `claude` binary directly and is unaffected either way. `claude-code-sdk` is frozen at 0.0.25 but still works because it spawns the same `claude` CLI under the hood. Migrating to `claude-agent-sdk` carries behavioural changes beyond the rename (`ClaudeCodeOptions` → `ClaudeAgentOptions`; the default system prompt no longer includes Claude Code's preset unless requested; settings sources are not loaded by default — verify against the installed version), which could change how the `sdk` runner behaves. Therefore: fix the field bugs in place, pin `claude-code-sdk==0.0.25`, add a preflight note ("`sdk` runner uses the frozen claude-code-sdk; migration planned for 1.1"), and document the migration path in the runbook. Re-evaluate if the pinned SDK breaks against a newer CLI. Record as ADR-014. Add a test with a fake `query()` yielding a realistic `ResultMessage`.
*Effort:* S (M if migration is pulled forward).

**P-12 — `FileNotFoundError` from a missing `cwd` is reported as a missing binary** (`subprocess_runner.py:100-105`, `codex_runner.py:110-115`).
*Fix:* check `Path(cwd).is_dir()` before `subprocess.run`; distinguish the two errors.
*Effort:* S.

**P-13 — SDK timeout yields an empty error string** (`str(asyncio.TimeoutError())`).
*Fix:* catch `asyncio.TimeoutError` explicitly and report `"Claude Code SDK timed out after Ns"`; ensure the SDK subprocess is terminated on timeout.
*Effort:* S.

**P-14 — Rate-limit heuristics are too broad.** `"429"`, `"quota"`, `"rate limit"` as substrings of stdout+stderr (`_rate_limit.py:42-53`) misclassify a generator that exits non-zero while *implementing* a rate limiter. ADR-011 explicitly wants conservative rotation.
*Fix:* match hints only against **stderr** and the last ~20 lines of stdout; require word boundaries (`\b429\b`); drop bare `"quota"` unless accompanied by "exceeded"/"limit". Keep the strict reset-time parser as the primary signal. Add negative tests.
*Effort:* S.

**P-15 — Runner timeout is a hard-coded 600 s** (`runners/base.py:183`, `generator.py:153-156`). A single feature implementation routinely exceeds 10 minutes; on timeout the error text is fed to the evaluator as a "self-evaluation".
*Fix:* `runner_timeout_seconds` in config (default 1800), per-role override for planner/evaluator; on timeout, record a failed iteration with a clear reason instead of invoking the evaluator.
*Effort:* S.

**P-16 — SDK `max_turns=50` is hard-coded** (`sdk_runner.py:102`).
*Fix:* `sdk_max_turns` in config (default 100).
*Effort:* S.

### D. Config, packaging, dependencies

**P-17 — Non-editable installs are broken.** `pyproject.toml` ships only `packages = ["harness"]` but the entry point is `cli:main`; the built wheel has no `cli.py`, so `pip install .` yields `ModuleNotFoundError: No module named 'cli'`.
*Fix:* move `cli.py` to `harness/cli.py`, entry point `harness.cli:main`; add a packaging test (`python -m build` + install into a temp venv + `harness --help`).
*Effort:* S.

**P-18 — `orchestration_mode` defaults to `"api"` in `HarnessConfig` (`config.py:131`) while README, CLI and ADR-010 say the default is `runner`.** `examples/*.yaml` load as api mode with `code_runner=None`.
*Fix:* default `"runner"`; add `code_runner: subprocess` and `orchestration_mode: runner` to the examples; README "write it manually" section updated.
*Effort:* S.

**P-19 — API-key fields can leak to GitHub.** `openai_api_key / gemini_api_key / openrouter_api_key` (`config.py:200-202`) and `RunnerProfile.env` accept literal secrets; `harness_config.json` sits in the output repo, is `git add -A`'d by the initializer and the generator, and pushed by `project_git.py`. US-21 intends these fields as documentation only, and US-23 already prefers `$ENV_VAR` references.
*Fix:* validate on load/save — these fields and any `env` value whose key looks secret (`*_KEY`, `*_TOKEN`, `*_SECRET`) must be a `$VAR`/`${VAR}` reference; a literal value is a hard error with a one-line fix hint. Add `.auto_resume.sh`, `.auto_resume.ps1`, `auto_resume.log` to the output project's `.gitignore` (written by the initializer).
*Effort:* S.

**P-20 — `EvaluatorWeights.validate_sum()` never called.** (Folded into P-06.)

**P-21 — `apply_setup_defaults` silently flips a project to runner mode** (`runner_profiles.py:87-89`) whenever `~/.harness/setup.json` exists, overriding a project's `orchestration_mode: api` on `run`/`resume`/`init`/`plan`.
*Fix:* only set `orchestration_mode` when the project config did not set it explicitly (track "explicitly set" via `model_fields_set`); print what changed.
*Effort:* S.

**P-22 — Profiles inherit the base `code_runner_model` across runner types** (`runner_profiles.py:146`): a Claude alias such as `opus` is passed to Codex as `--model opus`.
*Fix:* inherit only when `profile.runner == config.code_runner`; otherwise fall back to the runtime's own default.
*Effort:* S.

**P-23 — Declared but unused dependencies:** `gitpython`, `jinja2`. Remove. Unused imports (6), duplicate `datetime` import in `orchestrator.py:20/185`, f-strings without placeholders — run `ruff --fix`.
*Effort:* S.

**P-24 — `datetime.utcnow()` is deprecated on 3.12** (models, tracker, initializer, context). Replace with `datetime.now(timezone.utc)`; keep serialized format compatible with existing `features.json` files (add a tolerant parser for naive timestamps).
*Effort:* S.

### E. Preflight, prompts, UX

**P-25 — Preflight is skipped when `runner_profiles` is configured** (`orchestrator.py:640-656`).
*Fix:* run `preflight()` for the first profile of every role that will be used (dedupe by runner type); print the rotation panel *and* the readiness banner.
*Effort:* S.

**P-26 — Session opener contradicts the task.** The checklist says "pick the highest-priority `pending` feature" (`session/opener.py:91`) while the task block names a specific feature that is already `in_progress`.
*Fix:* checklist step 6 becomes "work only on the feature named in *Your task* below; do not start other features."
*Effort:* S.

**P-27 — The latest handoff is injected into every feature forever** (`orchestrator.py:339`). Handoffs are per-reset; a handoff about feature f3 is still shown when working on f9.
*Fix:* include a handoff only while `handoff.feature_id` (add the field) matches the current feature, or for the first feature after a reset.
*Effort:* S.

**P-28 — Minor correctness.** `base.py:121` overwrites `full_text` for multiple text blocks (should concatenate); `max_tokens=8096` → 8192; `harness/__init__.py` eagerly imports `Orchestrator` (slow `import harness.config`, circular-import risk) → make the package init lazy; `import_repo._count_code_files` walks excluded directories (`rglob` does not prune) → use `os.walk` with pruning.
*Effort:* S.

### F. Tests, CI, docs

**P-29 — Four orchestrator tests depend on `claude` being on PATH** (`tests/test_orchestrator.py:89` etc. — `Orchestrator.__init__` runs real preflight). Removing `claude` from PATH fails them.
*Fix:* fixture that patches `shutil.which` / runner preflight; CI must run with no coding-agent binaries installed.
*Effort:* S.

**P-30 — No CI.** `docs/diagrams/06_cicd_pipeline.drawio` describes a pipeline that does not exist.
*Fix:* GitHub Actions: `ruff check`, `pytest`, packaging test (P-17), on Python 3.11 and 3.12, Ubuntu + macOS.
*Effort:* S.

**P-31 — README drift.** "99+ tests" (185); project structure lists 4 of 15 test files; "Every runner validates itself" (P-25); "score ≥ threshold → git commit" (C-02); Key Design Decisions table lists mechanisms that are inert by default (C-01, C-03).
*Fix:* rewrite after Phases A–D so every claim is demonstrably true in the default configuration. Add a **Safety** section: all runners run with permissions bypassed in the output directory; recommend a container/VM or dedicated user for unattended runs.
*Effort:* S (after the code work).

**P-32 — Stray files at repo root:** `development_path.txt`, `documentation_init.txt`, `diagram_init.txt` are personal notes/prompts. Move to `docs/notes/` or remove.
*Effort:* S.

---

## 4. Planned capabilities to complete

**C-01 — Make context reset real (F-08, ADR-001, success metric "100 % recovery").**
Today `total_tokens` is never incremented for the default runners. Complete the mechanism as designed:
1. Subprocess runner: invoke `claude --print --output-format json`, parse `usage`, `total_cost_usd`, `is_error`, `result`, `session_id`; populate `RunResult` (fixes F-05's "None for subprocess" limitation — the data *is* available).
2. SDK runner: P-11.
3. Codex runner: parse `--json` event stream or `--output-last-message <file>` (verify flags for the installed version) for the final message; token usage if exposed.
4. Orchestrator: `should_reset()` now fires on real numbers; on reset, write the handoff, rebuild the preamble, reset the counter — already coded, now reachable.
5. Test: a simulated run crossing the threshold produces `handoff_session_0002.json`, the next iteration's preamble contains it, and no feature changes status as a side effect.
*Effort:* M.

**C-02 — Harness-owned commit per passing feature (US-08, PRD P1 "recovery point").**
After `result.passed`, if the working tree is dirty, Harness runs `git add -A && git commit -m "feat(<id>): <name> — score <x>"` itself (the generator may still commit; this guarantees the recovery point). Record the commit hash in a new `Feature.commit_sha` field, and record `Feature.base_commit_sha` (HEAD when the feature was started) so the pre-feature state is always known even if the generator never committed.
*Decision D-03 — DECIDED: revert to the last stable state, but keep the failed attempt for later reference.* When a feature exhausts its retry budget:
1. `git branch harness/attempt/<feature-id>/<n> HEAD` — the attempt's code (including the generator's intermediate commits) is preserved on a branch, never deleted.
2. Write `.harness/attempts/<feature-id>.md`: iteration count, each evaluator's scores and feedback, the generator's last self-evaluation, the failure pattern, and the branch ref.
3. `git reset --hard <base_commit_sha>` on the working branch so the next feature starts from a clean, passing baseline.
4. Any later attempt at that feature (after `resume`, a raised iteration budget, or a hand-edited description) injects the attempts file into the generator's prompt as "lessons from previous attempts", so it does not start from zero.
5. The final panel and `progress.md` list exhausted features with a pointer to their attempts file and branch.
Record as ADR-015.
*Effort:* M.

**C-03 — Sprint contract negotiation in runner mode (US-05, ADR-006).**
Replace the stub in `generator.py:100-105` with a runner call that returns `<sprint_contract>` XML (acceptance criteria + out of scope), parsed like the evaluator's block; the evaluator prompt already consumes the contract. Keep `sprint_contract_enabled` to skip the extra call. Fix the `_CONTRACT_SYSTEM` wording ("you are reviewing" → "you are proposing").
*Effort:* S–M.

**C-04 — Cost tracking per session (PRD P2).**
With C-01/P-11 data available: accumulate into `ProjectProgress.total_tokens_used` and `total_cost_usd` (fields exist, never written), per-feature totals in `EvaluationResult` or `Feature`, show in `harness status`, `progress.md`, and the final panel. Subscription runs show tokens with "cost: subscription".
*Effort:* S.

**C-05 — API-mode evaluator can inspect the project (ADR-002 "structured tool-use scoring").**
In `orchestration_mode: api` the evaluator is a single `messages.create` with only `submit_evaluation`; its system prompt asks it to run the startup command and navigate the UI, which it cannot do, so it grades the generator's self-report — the exact bias ADR-002 set out to remove.
*Decision D-04 — DECIDED: implement (a).* Add a bounded tool loop to `BaseAgent._call` for api mode: tools `read_file`, `list_files`, `search`, `run_command` (allow-listed commands: the startup command, the project's test command, `git diff`, `git log`), a maximum number of tool rounds (`api_tool_rounds`, default 20), then `submit_evaluation` is required. The permission boundary is the allow-list; *which* tools to use, and in what order, is the evaluator's own judgement — do not script a fixed inspection sequence. Reuse the same loop for the planner/initializer in api mode where it helps (reading an imported repo). Record as ADR-016.
*Effort:* L.

**C-06 — Role-appropriate permissions in runner mode.**
The evaluator "never writes code" (`evaluator.py:9`) but runs with permissions bypassed in the project directory. Where the runtime supports it, restrict: Claude Code evaluator/planner sessions with `--allowedTools` limited to Read/Grep/Glob plus allow-listed Bash (startup command, tests, `git diff/log`); Codex with `--sandbox read-only` (plus the commands it needs — verify what the sandbox permits). Generator and initializer keep write access. Make the tool lists configurable per role.
*Effort:* M.

**C-07 — Playwright-based verification for the evaluator (PRD P2, optional for 1.0 "done").**
Runner mode: pass `--mcp-config` with a Playwright MCP server to the evaluator session when `evaluator_browser_verification: true`; preflight checks the server is launchable; prompt instructs the evaluator to exercise acceptance criteria in the browser and report what it observed. Mark as *experimental* in README. May be deferred to a 1.1 release; if deferred, remove the Playwright sentence from the evaluator prompt.
*Effort:* L.

**C-08 — Validate the experimental scheduling backends (ADR-012).**
Run the systemd user-timer backend on a real Linux host and the Task Scheduler backend on Windows after P-01; record results in `docs/operational/runbook.md`; promote to "supported" only when a real resume completed. Known risks: `systemctl --user` needs a D-Bus session (headless/SSH hosts), `Persistent=false` loses the run if the machine sleeps through the fire time (consider `Persistent=true`), `schtasks` quoting.
*Effort:* M (needs hosts).

**C-09 — Resume reports what it is resuming.**
`harness resume` prints: features exhausted / in progress / pending, the feature it will pick up, and whether a handoff exists. Pairs with P-02.
*Effort:* S.

**C-10 — Protect the state files from the agents.**
`features.json` is both the harness's source of truth and a file the generator can edit (it works in the same directory with full permissions). Instruct agents not to modify harness state files; after every runner call, verify `features.json` against a hash taken before the call and restore the last known-good copy if it changed, logging a warning. Keep runtime junk (`.auto_resume.*`, `auto_resume.log`) out of git (P-19).
*Effort:* S.

---

## 5. Definition of Done — Harness 1.0

1. All `P-xx` items fixed, each with a regression test.
2. `C-01` through `C-06`, `C-09`, `C-10` complete. `C-07` and `C-08` either complete or explicitly listed as deferred/experimental in README and the runbook.
3. Decisions D-01 … D-04 (all decided — see P-09, P-11, C-02, C-05) recorded in `docs/technical/ADR.md` as ADR-013 … ADR-016.
4. `pip install .` into a clean virtualenv → `harness --help` works.
5. `pytest` passes on a machine with **no** `claude`, `codex` or `gh` on PATH.
6. CI green on Python 3.11 and 3.12, Ubuntu and macOS.
7. Manual end-to-end on a clean machine with only `claude` and `gh` installed:
   - `harness new --claude-code` with a toy brief completes ≥ 3 features; the output repo has one Harness commit per passing feature.
   - Kill the process mid-feature; `harness resume <dir>` run from a *different* working directory resumes the same feature.
   - Inject a fake "You've hit your limit · resets …" into a runner result; a launchd job is scheduled and, when fired, resumes correctly.
   - Set `context_reset_threshold_tokens` low; a handoff file appears and the following iteration's prompt contains it; no feature status changes as a side effect.
   - `harness status` shows tokens and cost.
8. README claims match behaviour in the default configuration; the Key Design Decisions table only lists mechanisms that are active by default.
9. Success metrics are measurable: context-reset recovery test (100 %), per-feature cost visible, actionable runner errors (P-12 test).

---

## 6. Suggested work order

| Phase | Items | Why in this order |
|---|---|---|
| **A — Infrastructure** | P-10 P-11 P-12 P-13 P-14 P-15 P-16 P-17 P-19 P-22 P-23 P-24 P-29 P-30 C-08 | Thesis-neutral layer. **The Harness 2.0 fork is seeded from here**, so fix once. Tag `v1.0-a` at the end of this phase = fork point. |
| **B — State and resume** | P-01 P-02 P-03 P-04 C-02 C-09 C-10 | Makes "fully restartable" true. |
| **C — Evaluation integrity** | P-05 P-06 P-07 P-08 P-09(D-01) C-03 C-06 C-05(D-04) | Makes the GAN gate real. |
| **D — Context and cost** | C-01 C-04 | Depends on A (token data). |
| **E — Polish and docs** | P-18 P-21 P-25 P-26 P-27 P-28 P-31 P-32, C-07 (stretch) | README rewrite last, once behaviour is final. |

Each phase ends with the full test suite green and a tag.

---

## 7. Verification notes

- **Runners:** fake binaries on PATH (shell scripts) that echo canned JSON / rate-limit text, so stdin handling, JSON parsing, `E2BIG` avoidance (prompt > 200 KB) and rate-limit classification are tested without real agents.
- **Resume:** tests must create configs with *relative* `output_dir` and run with a different `cwd`; today every test uses `tmp_path` absolute paths, which is how P-01 went unnoticed.
- **Evaluation:** table-driven tests for `_parse_runner_scores` and the api-mode path — lying overall score, out-of-range values, malformed numbers, missing block, functionality floor.
- **Packaging:** build wheel → install in temp venv → `harness --help`, in CI.
- **Auto-resume:** mock `subprocess.run` for scheduler calls; one real launchd run on macOS recorded in the runbook; Linux/Windows per C-08.

---

## 8. Out of scope for 1.0 (belongs to Harness 2.0)

Jira or any external backlog as source of truth · orchestrator / coder role split · human escalation tickets and non-blocking scheduling · milestone acceptance by a human · event log and dashboard · agent-runtime × model-provider connector matrix (Gemini CLI, Copilot CLI, …) · orchestrator-driven spec extension · ingestion of externally produced kickoff documents. None of these should be started in the 1.0 tree; the fork point is the end of Phase A.

---

## 9. Implementation record (2026-10-11)

Branch `harness-1.0`, one commit and tag per phase: `v1.0-a` (fork point for Harness 2.0), `v1.0-b`, `v1.0-c`, `v1.0-d`, `v1.0-e`. Suite: 360 passed, 3 skipped (macOS-only launchd tests and the opt-in packaging build), also with `claude`, `codex` and `gh` removed from `PATH`.

**Scope notes**

- Pulled forward: the runner half of C-01 (JSON / event-stream parsing) went into Phase A with P-10, since both rewrite the runners; P-26 and P-27 went into Phase B with the feature-loop rewrite.
- C-07 is implemented as **experimental** (runner mode, Claude Code runners only, off by default); the Playwright sentence appears in the evaluator prompt only when the session really has the browser.
- C-08: Task Scheduler validated on a real Windows host (runbook "Auto-resume backends"); systemd has **not** run on a real Linux host and stays experimental; launchd was not re-run on a real Mac in this pass (its Darwin-only tests run in CI on macOS).
- Found and fixed along the way: Windows `.cmd` shims not found by bare name (`codex`, `npm`); Rich swallowing `[f1]` in `status`; git operations that could hit a *parent* repo when a project's own `git init` failed (now restricted to the project's repo root); scheduler wrappers lacking `PATH`; `schtasks /SD` depending on the system locale; BOM-less PowerShell wrappers mangling non-ASCII paths.
- Verified against the installed CLIs: `claude` 2.1.296 (stdin prompt, `--output-format json` shape, `--permission-mode dontAsk` + `--allowedTools` enforcement) and `codex-cli` 0.160 (`exec -`, `--json` events, `--output-last-message`, `--sandbox read-only`).

**Review pass.** An independent review of the diff found 12 issues, all fixed with regression tests (`tests/test_review_fixes.py`): the exhausted-attempt follow-up commit could stage files the attempt had git-ignored (e.g. `.env`); a stale base commit could revert another feature's passing commit; wildcard git rules let read-only sessions write files (`git log --output=`); agents could loosen grading via `harness_config.json`; config reloads dropped `--runner`/`--model` overrides; timeouts did not kill process trees behind Windows `.cmd` shims; SDK rate-limit detection scanned the transcript; malformed api-mode tool input crashed the run; an evaluator outage burned every budget and reverted ungraded work; typographic apostrophes broke the PowerShell wrapper; credentials in git remotes / CLI args were not caught; `harness run file.yaml` left no resumable project config (pre-existing).

**Definition of Done (§5)**

| # | Item | Status |
|---|---|---|
| 1 | All P-xx fixed, each with a regression test | done |
| 2 | C-01…C-06, C-09, C-10 complete; C-07/C-08 complete or listed as experimental | done (C-07 experimental; C-08 partial, documented) |
| 3 | ADR-013…ADR-016 recorded | done |
| 4 | `pip install .` into a clean venv → `harness --help` | done locally (wheel → fresh venv); also a CI step |
| 5 | `pytest` passes with no `claude`/`codex`/`gh` on PATH | done locally; CI removes them before testing |
| 6 | CI green on 3.11/3.12, Ubuntu/macOS | workflow added; not run yet (branch not pushed) |
| 7 | Manual end-to-end on a clean machine | **not done** — needs real agent runs on a subscription; the scheduled-resume part was exercised (Windows) |
| 8 | README claims match default behaviour | done |
| 9 | Success metrics measurable | done: reset/handoff test, per-feature cost in `harness status`, runner-error tests |
