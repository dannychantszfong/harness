# Harness 1.0 — Review pass 2 (acceptance review of branch `harness-1.0`, 2026-10-11)

**Reviewed:** `harness-1.0` @ `9dc6dd8` (tag `v1.0`), diff against `main` (6 commits, 52 source/test files, +6.5k/−0.8k lines).
**Verdict:** the plan in `docs/HARNESS_1.0_POLISH.md` is implemented faithfully and the implementer's claims reproduce. **Not yet ready to tag as the Harness 2.0 fork point**: an independent review found one data-loss path and one process-orphaning regression in ordinary situations, plus a missing single-instance guard. All are small fixes. Six items below (R-01 … R-06), then two notes, then what remains for the owner.

---

## 1. What was verified (all reproduce)

| Claim | Result |
|---|---|
| `pytest`: 360 passed, 3 skipped | ✅ reproduced (skips: 2 launchd-on-macOS, 1 opt-in packaging) |
| `ruff check .` clean (project config E4/E7/E9/F/W/DTZ) | ✅ |
| Suite passes with `claude`, `codex`, `gh` removed from PATH | ✅ reproduced |
| Wheel → fresh venv → `harness --help` | ✅ reproduced (`HARNESS_PACKAGING_TESTS=1`, 3 passed) |
| Every defect from review pass 1 | ✅ all 21 replay checks pass: absolute `output_dir` + resume from the project dir; interrupted feature resumes first; Harness computes the score, lying `overall_score` ignored (3/3/3/2 → 2.85, fail); functionality floor (9/9/9/3 → 8.1 but fail); parse failure flagged, no 5.0 sentinel; out-of-range clamped; malformed numbers do not crash; SDK `is_error`/`usage` dict/`total_cost_usd`/`result` read correctly; 300 KB prompt via stdin; subprocess usage + cost parsed; missing `cwd` no longer reported as "binary not found"; literal secrets rejected, `$VAR`/`${VAR}` accepted; `orchestration_mode` default `runner`, examples load as runner mode |
| ADR-013 … ADR-016 recorded; §9 implementation record present and candid | ✅ |

The working tree in the zip shows 63 "modified" files — all CRLF-only noise from the Windows checkout (`git diff --ignore-cr-at-eol` is empty). See R-07.

---

## 2. Required before the fork tag

**R-01 — HIGH — Exhaustion reset discards a PASSING feature whose Harness commit failed.**
`harness/orchestrator.py:698-703` (`_commit_passing_feature` only warns when `commit_all` fails, feature stays PASSING with `commit_sha=None`), `:467-472` (next feature records `base_commit_sha = HEAD`, which predates the uncommitted passing work), `:804-829` (`_revert_blocker` protects only features that have a `commit_sha`), `:750` (`reset_hard`).
*Scenario (reproduced):* f1 passes → the harness commit is rejected (pre-commit hook such as husky/lint-staged; or the process is killed between `record_evaluation(passed)` and `_commit_passing_feature`) → f1 PASSING, files uncommitted → f2 starts with base = HEAD → f2 exhausts → `commit_all` sweeps f1's files into the attempt commit → `reset --hard base` → `f1.py` is gone from the working branch while `features.json`, `progress.md` and the final panel report f1 as passing. Recoverable by hand from `harness/attempt/f2/1`, but it violates ADR-015 and the "no features lost" metric.
*Fix:* (a) in `mark_in_progress` with `fresh_base=True`, if the tree is dirty, commit first (`chore(harness): snapshot before <id>`) or refuse to record a base; (b) `_revert_blocker` treats any PASSING feature without `commit_sha` as a blocker; (c) on resume, reconcile PASSING-without-`commit_sha` by committing the tree and recording the SHA. Regression test with a rejecting pre-commit hook.

**R-02 — HIGH — Agent sessions survive the death of the harness (regression introduced in Phase A).**
`harness/runners/base.py:345` (`start_new_session=True`), `:366-371` (only `BaseException` raised inside Python — i.e. `KeyboardInterrupt`/timeout — kills the tree).
*Scenario (reproduced):* SIGTERM or SIGHUP to the harness process group (terminal closed, `kill`, scheduler stop, job control) → harness dies, `claude --dangerously-skip-permissions` / `codex --dangerously-bypass-approvals-and-sandbox` keeps running in its own session and keeps editing the project. `harness resume` then starts a second agent on the same tree. On `main` (plain `subprocess.run`) the child died with the harness.
*Fix:* install SIGTERM/SIGHUP handlers at harness start that raise `SystemExit` (so the existing `BaseException` path calls `kill_process_tree`), and on Linux set `prctl(PR_SET_PDEATHSIG, SIGKILL)` in `preexec_fn`; on Windows use a Job Object with kill-on-close if feasible, otherwise document. Test: send SIGTERM to the parent while a fake agent sleeps; assert the child is gone.

**R-03 — MEDIUM — No single-instance guard; a manual `harness resume` never cancels the scheduled auto-resume job.**
`harness/cli.py` (`resume`/`run`), `harness/auto_resume.py:92` (`cancel` has no callers).
*Scenario:* cap hit 14:00, job scheduled 19:05; owner resumes manually at 18:00 → at 19:05 a second harness starts in the same directory: two generators, two writers of `features.json`, and `commit_all`/`reset --hard` from one instance racing the other's session.
*Fix:* `auto_resume.cancel(project_id)` at the start of `run`/`resume`; a lock file (`.harness/run.lock` with pid + start time, stale-lock detection) acquired by `run`/`resume`; refuse to start when held.

## 3. Recommended (fix in the same pass)

**R-04 — MEDIUM-LOW — Nested git repositories created by an exhausted attempt survive the reset and are committed as dangling gitlinks.** `harness/project_git.py:112-121` (`git add -A`), `orchestrator.py:750`. Scaffolders (`rails new`, some templates) run `git init` in a subdirectory; `add -A` records a gitlink, `reset --hard` cannot remove a nested repo, and the next passing commit carries an `app` submodule pointer with no `.gitmodules` — clones break. *Fix:* before `commit_all`/`add -A`, detect `*/.git` below the project root (excluding the root), and either remove the nested `.git` directory (treat the files as plain files) or exclude the path and warn; add to `keep_excluded` handling.

**R-05 — LOW-MEDIUM — Generator runner failures other than timeout/rate-limit are graded and burn the feature budget.** `harness/agents/generator.py:278` returns `"[Runner error] …"` as the self-evaluation; each failure costs an iteration, so a broken runner (missing binary at run time, auth failure, unresolved `$VAR` in a profile env when launched by a scheduler wrapper) marches every feature to exhaustion, with a `wip` commit, attempt branch and reset per feature. The evaluator already has an outage guard; the generator needs the same: classify `runner_error` (not timeout, not rate-limit) → do not count the iteration, stop the run with the feature left IN_PROGRESS, print the cause.

**R-06 — LOW — API-mode `search` tool follows symlinks out of the project.** `harness/agents/tools.py` (`_walk`/`search`): `read_file`/`list_files` refuse `link.txt -> ../outside.txt`, `search` returns its contents. *Fix:* resolve each candidate and require it to be inside the resolved root (same check `read_file` uses); skip symlinked directories. Also reject `-O<file>` option bundling in the git argv filter (harmless today, cheap to close).

## 4. Notes (no action required to tag, but record them)

**N-01 — Secret-literal rejection is a name heuristic.** `harness/config.py:21,45`: keys not ending in KEY/TOKEN/SECRET/PASSWORD (`APIKEY`, `GHTOKEN`, `AUTH`, `CREDENTIALS`, `DATABASE_URL` with an embedded password) and env carried inside CLI args (`--settings '{"env":{…}}'`, codex `-c …api_key=…`) pass. `$VAR` handling has no false positives. Consider widening the pattern (`API_?KEY|AUTH|CREDENTIAL|PASSWD|_URL` with `://…:…@`) and scanning `code_runner_extra_args` values for `sk-`/`ghp_`-style prefixes. Document the limits in README "Safety".

**N-02 — Read-only Claude Code sessions rely on permission rules, not an OS sandbox.** `harness/runners/base.py:187-199`: `Bash(<test command> *)` lets the evaluator pass arbitrary arguments to the test runner (`pytest --junitxml=../x.xml`), and whether Claude Code refuses `git log --output=<file>` under `--permission-mode dontAsk` is Claude Code behaviour the suite cannot test (no binary in CI). The implementer verified the flag behaviour against `claude` 2.1.296 manually — record the version in the runbook and re-check on CLI upgrades. Codex's `--sandbox read-only` is OS-enforced.

**R-07 — hygiene:** add `.gitattributes` (`* text=auto eol=lf`) so Windows checkouts stop showing 63 CRLF-modified files; `ci.yml` triggers only on `push: main` and PRs — add the `harness-1.0` branch (or `branches: ['**']`) so the first push actually runs CI; `LS` in the read-only tool list is not a current Claude Code tool name (harmless).

---

## 5. After the fixes

1. Re-run the full suite, the PATH-stripped suite and the packaging test.
2. **Move the fork tag:** R-02 lives in `runners/base.py`, i.e. inside the Phase A layer that Harness 2.0 inherits. Re-tag `v1.0-a` on the commit that contains R-02 (nothing is pushed, so re-tagging is safe), and re-tag `v1.0`.
3. Push the branch so CI runs (DoD 6).
4. The one check no reviewer can do for you (DoD 7): a real run on your subscription — `harness new --claude-code` on a toy brief for ≥ 3 features; kill it mid-feature and `harness resume` from another directory; then simulate a rate-limit message and let the scheduled job resume it. Everything else in the Definition of Done is met.

---

## 6. Resolution (2026-10-11)

| Item | Status | Change | Regression tests (`tests/test_review_pass2.py`) |
|---|---|---|---|
| R-01 | fixed | `Orchestrator._fresh_base`: commit what is on disk (other than Harness state files) before recording a base; record that commit for passing features without `commit_sha`; no base if the commit fails. `_revert_blocker` blocks while a passing feature has no commit. Louder "Recovery point not recorded" panel. ADR-015, runbook INC-20 | rejecting pre-commit hook (first commit only / every commit); Harness killed between pass and commit; state-only changes need no snapshot; blocker unit test |
| R-02 | fixed | `harness/process_guard.py`: SIGTERM/SIGHUP → `SystemExit` (existing kill-the-tree path), ignored signals left alone; Windows kill-on-close job object. `runners.base.child_popen_kwargs`: own session + Linux `PR_SET_PDEATHSIG`. ADR-017 | terminate the fake Harness while a fake agent and its grandchild sleep (SIGTERM on POSIX, TerminateProcess on Windows); Linux SIGKILL; `nohup` SIGHUP stays ignored; guard installed only by agent commands |
| R-03 | fixed | `harness/run_lock.py`: OS lock on `.harness/run.lock` (flock / byte-range lock), taken by the CLI before any write and held by `Orchestrator.run`; `auto_resume.cancel_pending` at run start, skipped inside the scheduled job (`HARNESS_AUTO_RESUME_JOB`). Lock file gitignored, never snapshotted. ADR-017, runbook INC-18 | second process refused, lock freed when the holder is killed; reentrant hold; `resume` refuses without touching the config; pending job cancelled; the job does not cancel itself; lock never committed |
| R-04 | fixed | `project_git.absorb_nested_repos` before every `git add -A`: nested `.git` moved to `<git-dir>/harness/nested-repos/`, staged gitlinks without `.gitmodules` unstaged; declared submodules untouched | nested repo committed as files; nested repo from an exhausted attempt removed by the reset; what the nested repo's own `.gitignore` excluded stays out of the next passing commit; submodules left alone |
| R-05 | fixed | `RunnerFailedError` from the generator → retried once without counting, then "Stopped — generator unavailable" with the feature in progress. `RunResult.turn_limited` / `RunnerTurnLimitError`: a turn-limit end is a counted, ungraded failure like a timeout. Runbook INC-19 | outage stops after two attempts with nothing counted; one failure retried at the same iteration; turn limit recorded; generator raises the right errors |
| R-06 | fixed | `ProjectTools.search` checks each file's real path; `_forbidden_git_option` refuses short-option bundles containing `O` and abbreviations of refused long options (`--text` stays allowed) | symlink not followed (skipped where symlinks are not permitted); bundles/abbreviations refused; ordinary options pass |
| R-07 | fixed | `.gitattributes` `* text=auto eol=lf`; CI `push` also on `harness-*`; `LS` removed from `default_read_only_tools` | tool list |
| N-01 | done | wider secret names, credential-shaped values, URLs with a password, secrets inside extra args; limits documented in README "Safety"; `--max-tokens` no longer a false positive | widened rejections; ordinary settings still load |
| N-02 | done | runbook "Read-only sessions: verified CLI behaviour" (claude 2.1.296, codex-cli 0.160, re-check steps, process-lifetime limits); README "Safety" | — |

After the fixes:

Follow-up (owner's replay of R-04): `_handle_exhausted` recorded the git-ignored untracked paths *before* absorbing nested repos, so a nested repo's own ignores (`app/node_modules`) were missed; after the reset removed `app/.gitignore`, the next passing commit swept `node_modules` in. Nested repos are now absorbed first.

1. Suite 402 passed, 6 skipped; the same with `claude`/`codex`/`gh` removed from `PATH`; packaging (wheel → clean venv → `harness --help`) 3 passed. `ruff check .` clean.
2. `v1.0-a` and `v1.0` moved to the review-pass-2 commit (local tags only).
3. Push — left to the owner.
4. DoD 7 — left to the owner.
