"""Main orchestrator — ties all agents and harness components together.

Architecture (combining both articles):

  Initializer  ──► Planner  ──► for each feature:
                                  ├─ negotiate sprint contract
                                  └─ Generator ◄──► Evaluator  (GAN loop)
                                       ↓ pass
                                  git commit + progress update
                                  ↓
                              context reset if token budget exceeded
                                  ↓
                              HandoffDocument for next session

The orchestrator is the only component that knows the full picture.
Individual agents are stateless; all state lives in files on disk.
"""

import subprocess
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from harness.config import CONFIG_FILENAME, HarnessConfig
from harness.agents import InitializerAgent, PlannerAgent, GeneratorAgent, EvaluatorAgent
from harness.runners import create_runner, RunnerType
from harness.runners.base import RunnerRateLimitedError, RunnerTimeoutError
from harness import auto_resume
from harness.context.reset import ContextReset
from harness.context.handoff import HandoffDocument
from harness.progress.attempts import read_lessons, write_attempt_notes
from harness.progress.tracker import ProgressTracker, attempts_file_relpath, write_atomic
from harness.progress.models import EvaluationResult, FeatureStatus, ProjectProgress
from harness.project_git import (
    ProjectGitConfigurationError,
    commit_all,
    create_branch,
    ensure_project_gitignore,
    git_head,
    is_git_repo,
    reset_hard,
    sync_project_git,
)
from harness.runner_profiles import (
    runner_for_profile,
    role_profiles,
)
from harness.session.opener import SessionOpener

console = Console()


class Orchestrator:
    """Drives the full harness lifecycle for a project."""

    # Fields that are safe to live-reload from harness_config.json between seams.
    # Identity fields (project_id, output_dir, code_runner, orchestration_mode)
    # are intentionally pinned — changing them mid-run would invalidate the
    # runner instance and progress files.
    _LIVE_RELOAD_FIELDS: tuple[str, ...] = (
        "planner_model",
        "generator_model",
        "evaluator_model",
        "evaluator_pass_score",
        "evaluator_weights",
        "max_iterations_per_feature",
        "runner_timeout_seconds",
        "planner_timeout_seconds",
        "evaluator_timeout_seconds",
        "sdk_max_turns",
        "context_reset_threshold_tokens",
        "sprint_contract_enabled",
        "code_runner_model",
        "codex_oss",
        "codex_local_provider",
        "code_runner_extra_args",
        "progress_animation",
        "progress_phrase_style",
        "progress_text_effect",
        "project_git_push",
        "project_git_branch",
        "project_git_remote",
        "project_github_repo",
        "project_github_private",
        "runner_profiles",
        "planner_runner_order",
        "generator_runner_order",
        "evaluator_runner_order",
        "reviewer_runner_order",
        "fallback_on_rate_limit",
    )

    def __init__(self, config: HarnessConfig, runner_type: RunnerType | None = None) -> None:
        self.config = config
        self.tracker = ProgressTracker(config)
        self.context_reset = ContextReset(config)
        self.session_opener = SessionOpener(config)
        self.total_tokens = 0
        self.session_number = 1
        # Track harness_config.json mtime so we can pick up edits between seams.
        self._config_path = HarnessConfig.default_config_path(config.output_dir)
        self._config_mtime = (
            self._config_path.stat().st_mtime if self._config_path.exists() else 0.0
        )
        # Resolve runner: explicit arg > config value > default (subprocess)
        _rt = runner_type or RunnerType(config.code_runner or "subprocess")
        self.runner = create_runner(_rt, config)
        self._role_profile_index: dict[str, int] = {}
        self._role_runner_cache: dict[tuple[str, int], object] = {}
        self._print_runner_status()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        confirmed_spec: str | None = None,
        review_only: bool = False,
    ) -> None:
        """Run the harness end-to-end.

        Args:
            confirmed_spec: A spec already agreed with the user (from `harness new`).
                            When provided the planner phase is skipped entirely.
            review_only: Skip init/plan/loop and run the ReviewerAgent against
                            the project directory. Used by `harness import`
                            on repos that look done.
        """
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        console.print(
            Panel(
                f"[bold]Claude Agent Harness[/bold]\n{self.config.project_name}",
                subtitle=self.config.brief[:80],
            )
        )

        if review_only:
            try:
                self._review_only()
            except RunnerRateLimitedError as exc:
                self._handle_rate_limit(exc)
            return

        # Keep auto-resume wrappers and logs out of the project's commits
        # (also for projects created before the initializer wrote this).
        ensure_project_gitignore(output_dir)

        try:
            # Phase 1: Initialize (idempotent — skips if already done)
            progress = self._initialize(confirmed_spec=confirmed_spec)
            self._sync_project_git("initialization")

            # Phase 2: Plan (skip if a confirmed spec was supplied)
            progress = self._plan(progress, confirmed_spec=confirmed_spec)

            # Phase 3: Feature implementation loop
            self._feature_loop(progress)
        except RunnerRateLimitedError as exc:
            self._handle_rate_limit(exc)
            return

        # Reload final state from disk — _feature_loop mutates progress internally
        final = self.tracker.load()
        console.print(self._final_panel(final))

    def _final_panel(self, final: ProjectProgress) -> Panel:
        passing, total = len(final.passing_features), len(final.features)
        exhausted = final.exhausted_features(self.config.max_iterations_per_feature)
        if passing == total:
            lines = [f"[bold green]Done![/bold green] {passing}/{total} features passing."]
            title = "Harness Complete"
        else:
            lines = [f"[bold yellow]Finished.[/bold yellow] {passing}/{total} features passing."]
            title = "Harness Finished"
        if exhausted:
            lines.append(
                f"\n[yellow]{len(exhausted)} feature{'s' if len(exhausted) != 1 else ''} "
                "exhausted retries:[/yellow]"
            )
            for f in exhausted:
                branch = f.attempt_branches[-1] if f.attempt_branches else "no branch"
                lines.append(
                    f"  • {f.id} {f.name} — {attempts_file_relpath(f.id)} ({branch})"
                )
            lines.append(
                "\n[dim]Raise max_iterations_per_feature or edit the feature description, "
                "then `harness resume` to retry them with these notes.[/dim]"
            )
        return Panel("\n".join(lines), title=title)

    def _review_only(self) -> None:
        """Audit-only path: run ReviewerAgent, write REVIEW.md, exit clean."""
        from harness.agents.reviewer import ReviewerAgent
        console.print("\n[bold blue]Phase: Review[/bold blue]")
        # Load progress if features.json exists; otherwise proceed without it.
        progress = None
        try:
            progress = self.tracker.load()
        except Exception:
            pass

        self._with_role_fallback(
            "reviewer",
            lambda runner: ReviewerAgent(self.config, runner=runner).review(progress=progress),
        )

        review_path = Path(self.config.output_dir) / "REVIEW.md"
        console.print(
            Panel(
                f"[bold green]Review complete.[/bold green]\n"
                f"Findings written to [bold]{review_path}[/bold]",
                title="Harness Review",
            )
        )

    def _handle_rate_limit(self, exc: RunnerRateLimitedError) -> None:
        """Print a friendly notice and (optionally) schedule auto-resume."""
        from datetime import datetime, timezone
        body_lines = ["[yellow]Runner usage cap reached.[/yellow]"]
        if exc.reset_at is not None:
            local_reset = exc.reset_at.astimezone()
            wait = exc.reset_at - datetime.now(timezone.utc)
            hours, rem = divmod(int(wait.total_seconds()), 3600)
            minutes = rem // 60
            wait_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
            body_lines.extend([
                f"  Resets at [bold]{local_reset.strftime('%Y-%m-%d %H:%M %Z')}[/bold] "
                f"(in [bold]{wait_str}[/bold])",
                "",
            ])
        else:
            body_lines.extend([
                "  The runner did not report a reset time.",
                "",
            ])

        scheduled = None
        if exc.reset_at is not None and self.config.auto_resume_on_rate_limit:
            try:
                scheduled = auto_resume.schedule(
                    project_dir=Path(self.config.output_dir),
                    project_id=self.config.project_id,
                    fire_at_utc=exc.reset_at,
                )
            except Exception as e:  # scheduling is best-effort
                body_lines.append(f"[red]Could not schedule auto-resume:[/red] {e}")

        if scheduled and exc.reset_at is not None:
            backend = scheduled.get("backend", "scheduler")
            body_lines.append(
                f"[green]Auto-resume scheduled[/green] for "
                f"[bold]{scheduled['fire_local'].strftime('%H:%M %Z')}[/bold] "
                f"via {backend} label [dim]{scheduled['label']}[/dim]"
            )
            body_lines.append(f"  Log: {scheduled['log']}")
            if scheduled.get("cancel"):
                body_lines.append(f"  Cancel: [dim]{scheduled['cancel']}[/dim]")
        elif exc.reset_at is not None:
            body_lines.append(
                f"To continue manually after reset, run:\n"
                f"  [bold]harness resume {self.config.output_dir}[/bold]"
            )
        else:
            body_lines.append(
                "No reset time was reported. Fix auth/quota or wait for the "
                "provider cap to clear, then run:\n"
                f"  [bold]harness resume {self.config.output_dir}[/bold]"
            )

        console.print(Panel(
            "\n".join(body_lines),
            title="[yellow]Paused — rate limit[/yellow]",
            border_style="yellow",
        ))

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def _agent_runner(self, role: str = "planner"):
        """Return the runner to pass to orchestration agents (planner/evaluator/initializer).

        In 'runner' mode all agents share the same runner.
        In 'api' mode they get None and use the Anthropic API directly.
        """
        if self.config.orchestration_mode != "runner":
            return None
        if self.config.runner_profiles:
            return self._runner_for_role(role)
        return self.runner

    def _initialize(self, confirmed_spec: str | None = None) -> ProjectProgress:
        console.print("\n[bold blue]Phase 1: Initialize[/bold blue]")
        return self._with_role_fallback(
            "initializer",
            lambda runner: InitializerAgent(self.config, runner=runner).run(
                brief=self.config.brief,
                spec=confirmed_spec,
            ),
        )

    def _plan(
        self,
        progress: ProjectProgress,
        confirmed_spec: str | None = None,
    ) -> ProjectProgress:
        console.print("\n[bold blue]Phase 2: Plan[/bold blue]")

        if progress.spec:
            console.print("  Spec already exists, skipping planner.")
            return progress

        if confirmed_spec:
            console.print("  Using pre-confirmed spec from requirement alignment.")
            progress.spec = confirmed_spec
            self.tracker.save(progress)
            return progress

        # Resume case: spec.md was written during a prior `harness new` run
        # but progress.spec wasn't populated. Recover from disk.
        spec_path = self.config.spec_path
        if spec_path.exists():
            console.print(f"  Loading spec from {spec_path}.")
            progress.spec = spec_path.read_text()
            self.tracker.save(progress)
            return progress

        # Legacy resume: features already exist (alignment must have happened
        # in a prior session that didn't write spec.md). Re-running the planner
        # would discard that work, so we write a placeholder spec from the
        # brief and let the user edit spec.md if they want a richer one — the
        # config-reload-at-seams will pick the edit up automatically.
        if progress.features:
            console.print(
                "  [yellow]No spec.md found, but features already exist. "
                "Using project brief as a placeholder spec.[/yellow]"
            )
            console.print(
                f"  [dim]Edit {spec_path} to write a richer spec; "
                "the next session will pick it up.[/dim]"
            )
            placeholder = (
                f"# {self.config.project_name} — Placeholder Spec\n\n"
                f"_Original alignment was not preserved on disk. Edit this "
                f"file to write a richer spec; the harness picks up changes "
                f"between feature seams._\n\n"
                f"---\n\n## Brief\n\n{self.config.brief}\n"
            )
            spec_path.write_text(placeholder)
            progress.spec = placeholder
            self.tracker.save(progress)
            return progress

        holder = {}

        def _run_planner(runner):
            agent = PlannerAgent(self.config, runner=runner)
            holder["agent"] = agent
            return agent.run(brief=self.config.brief)

        spec = self._with_role_fallback("planner", _run_planner)
        progress.spec = spec
        self.tracker.save(progress)
        agent = holder.get("agent")
        if agent is not None:
            self._account_tokens(agent.usage.total_tokens)
        return progress

    def _feature_loop(self, progress: ProjectProgress) -> None:
        console.print("\n[bold blue]Phase 3: Feature implementation loop[/bold blue]")
        project_dir = Path(self.config.output_dir)
        # Continue the session numbering of earlier runs so a resumed run's
        # handoffs never overwrite older ones.
        self.session_number = max(progress.session_count, 1)
        # A handoff from an earlier run is only relevant to the feature it was
        # written for (P-27); _handoff_for() decides per feature.
        self._handoff = HandoffDocument.load_latest(project_dir)
        self._handoff_fresh = False

        while True:
            # Seam 1: between features — pick up any config edits before
            # negotiating the next sprint contract.
            self._reload_config_if_changed()

            feature = progress.next_actionable_feature(self.config.max_iterations_per_feature)
            if not feature:
                exhausted = progress.exhausted_features(self.config.max_iterations_per_feature)
                if exhausted:
                    console.print(
                        f"[yellow]No features left to attempt "
                        f"({len(exhausted)} exhausted their iteration budget).[/yellow]"
                    )
                else:
                    console.print("[green]All features are passing. Harness complete.[/green]")
                break

            resuming = feature.status != FeatureStatus.PENDING or feature.iteration_count > 0
            suffix = (
                f" — resuming at iteration {feature.iteration_count + 1}"
                if resuming else ""
            )
            console.print(f"\n[bold]Feature:[/bold] {escape(f'{feature.name} [{feature.id}]')}{suffix}")

            # Mark in progress; remember the pre-feature commit (ADR-015).
            progress = self.tracker.mark_in_progress(
                progress, feature.id, base_commit_sha=git_head(project_dir),
            )
            feature = progress.get_feature(feature.id)

            session_ctx = self._session_context(progress, feature)
            lessons = read_lessons(project_dir, feature.id)
            if lessons:
                console.print(f"  [dim]Injecting lessons from {attempts_file_relpath(feature.id)}[/dim]")

            # Negotiate sprint contract (once per feature)
            if self.config.sprint_contract_enabled and feature.sprint_contract is None:
                console.print("  Negotiating sprint contract...")

                def _negotiate(runner):
                    return GeneratorAgent(
                        self.config, runner=runner or self.runner,
                    ).negotiate_sprint_contract(
                        feature=feature,
                        spec=progress.spec or self.config.brief,
                    )

                contract = self._with_role_fallback("generator", _negotiate, protect_state=True)
                progress = self.tracker.attach_sprint_contract(progress, feature.id, contract)
                feature = progress.get_feature(feature.id)  # refresh

            # GAN-style generator ↔ evaluator loop. The budget is per feature
            # across runs: a resumed feature continues from its iteration
            # count. A while-loop (not range) so live edits to
            # max_iterations_per_feature take effect on the next iteration.
            latest = feature.latest_evaluation
            evaluator_feedback: str | None = (
                latest.feedback if latest is not None and not latest.passed else None
            )
            passed = False
            last_self_eval: str | None = None
            iteration = feature.iteration_count

            while iteration < self.config.max_iterations_per_feature:
                # Seam 2: between iterations within a feature — model swaps,
                # threshold changes, etc. take effect from the next call.
                self._reload_config_if_changed()
                iteration += 1
                console.print(f"  [cyan]Iteration {iteration}[/cyan]")

                # Generate
                gen_holder = {}

                def _generate(runner):
                    agent = GeneratorAgent(self.config, runner=runner or self.runner)
                    gen_holder["agent"] = agent
                    return agent.implement_feature(
                        feature=feature,
                        progress=progress,
                        session_preamble=session_ctx,
                        evaluator_feedback=evaluator_feedback,
                        iteration=iteration,
                        lessons=lessons,
                    )

                try:
                    self_eval = self._with_role_fallback("generator", _generate, protect_state=True)
                except RunnerTimeoutError as exc:
                    gen_agent = gen_holder.get("agent")
                    if gen_agent is not None:
                        self._account_tokens(gen_agent.usage.total_tokens)
                    result = self._timeout_result(exc, iteration)
                    console.print(f"  [red]✗ {exc} — iteration recorded as failed[/red]")
                    progress = self.tracker.record_evaluation(progress, feature.id, result)
                    feature = progress.get_feature(feature.id)
                    evaluator_feedback = result.feedback
                    continue
                last_self_eval = self_eval
                gen_agent = gen_holder.get("agent")
                if gen_agent is not None:
                    self._account_tokens(gen_agent.usage.total_tokens)

                eval_holder = {}

                def _evaluate(runner):
                    agent = EvaluatorAgent(self.config, runner=runner)
                    eval_holder["agent"] = agent
                    return agent.evaluate(
                        feature=feature,
                        generator_self_eval=self_eval,
                        iteration=iteration,
                    )

                result: EvaluationResult = self._with_role_fallback(
                    "evaluator",
                    _evaluate,
                    protect_state=True,
                )
                eval_agent = eval_holder.get("agent")
                if eval_agent is not None:
                    self._account_tokens(eval_agent.usage.total_tokens)

                self._print_eval_summary(result)

                # Record
                progress = self.tracker.record_evaluation(progress, feature.id, result)
                feature = progress.get_feature(feature.id)

                if result.passed:
                    console.print(f"  [green]✓ Passed (score {result.overall_score:.1f})[/green]")
                    passed = True
                    progress = self._commit_passing_feature(progress, feature.id, result.overall_score)
                    self._sync_project_git(f"feature {feature.id}")
                    break

                evaluator_feedback = result.feedback
                console.print(
                    f"  [yellow]✗ Score {result.overall_score:.1f} < {self.config.evaluator_pass_score} — iterating[/yellow]"
                )

                # Check if we need a context reset
                if self.should_reset():
                    console.print("  [red]Context budget exceeded — triggering reset[/red]")
                    self._handoff = self._do_context_reset(progress, feature.id, evaluator_feedback)
                    self._handoff_fresh = True
                    session_ctx = self._session_context(progress, feature)
                    self.total_tokens = 0

            if not passed:
                console.print(
                    f"  [red]Feature {feature.name} did not pass after "
                    f"{feature.iteration_count} iterations.[/red]"
                )
                progress = self._handle_exhausted(progress, feature.id, last_self_eval)

            progress = self.tracker.load()  # reload from disk


    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _account_tokens(self, tokens: int) -> None:
        self.total_tokens += tokens

    def _handoff_for(self, feature) -> HandoffDocument | None:
        """The handoff to show while working on `feature` (P-27).

        Handoffs are per reset: one written about f3 must not follow the run
        into f9. A handoff is shown while its feature_id matches; one without
        a feature (written at a feature seam) is shown for the first feature
        after that reset only.
        """
        handoff = self._handoff
        if handoff is None:
            return None
        if handoff.feature_id is not None:
            return handoff if handoff.feature_id == feature.id else None
        return handoff if self._handoff_fresh else None

    def _session_context(self, progress: ProjectProgress, feature) -> str:
        return self.session_opener.build_opening_context(
            progress=progress,
            handoff=self._handoff_for(feature),
            include_git_log=True,
        )

    # ── Git recovery points (C-02 / ADR-015) ──────────────────────────────

    def _commit_passing_feature(
        self, progress: ProjectProgress, feature_id: str, score: float,
    ) -> ProjectProgress:
        """Harness-owned recovery point for a passing feature.

        The generator may have committed already; this guarantees the commit
        exists even if it did not. Records HEAD as the feature's commit_sha.
        """
        feature = progress.get_feature(feature_id)
        project_dir = Path(self.config.output_dir)
        message = f"feat({feature.id}): {feature.name} — score {score:.1f}"
        try:
            sha = commit_all(project_dir, message)
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            console.print(f"  [yellow]Could not commit feature {feature.id}: {detail}[/yellow]")
            return progress
        if sha is None:
            console.print("  [yellow]Not a git repository — no recovery point recorded.[/yellow]")
            return progress
        console.print(f"  [green]Recovery point:[/green] {sha[:10]}  {message}")
        return self.tracker.record_commit(progress, feature.id, sha)

    def _handle_exhausted(
        self,
        progress: ProjectProgress,
        feature_id: str,
        last_self_eval: str | None,
    ) -> ProjectProgress:
        """D-03: preserve the failed attempt, revert to the last stable state.

        1. commit the attempt and point harness/attempt/<id>/<n> at it
        2. write .harness/attempts/<id>.md (scores, feedback, pattern, branch)
        3. git reset --hard <base_commit_sha> on the working branch — Harness
           state files are snapshotted first and restored afterwards, so the
           run's progress survives the reset
        """
        feature = progress.get_feature(feature_id)
        project_dir = Path(self.config.output_dir)
        attempt_number = len(feature.attempt_branches) + 1
        base = feature.base_commit_sha
        branch: str | None = None
        attempt_commit: str | None = None
        reverted = False

        if is_git_repo(project_dir):
            snapshot = self._snapshot_state_files()
            try:
                attempt_commit = commit_all(
                    project_dir,
                    f"wip({feature.id}): exhausted attempt {attempt_number} (preserved by Harness)",
                )
                if attempt_commit and attempt_commit != base:
                    branch = f"harness/attempt/{feature.id}/{attempt_number}"
                    create_branch(project_dir, branch, attempt_commit)
                    if base:
                        reset_hard(project_dir, base)
                        reverted = True
            except (subprocess.CalledProcessError, OSError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                console.print(f"  [yellow]Could not preserve/revert the attempt: {detail}[/yellow]")
            finally:
                self._restore_state_files(snapshot)

        notes = write_attempt_notes(
            project_dir,
            feature,
            attempt_number=attempt_number,
            max_iterations=self.config.max_iterations_per_feature,
            branch=branch,
            base_commit=base if reverted else None,
            attempt_commit=attempt_commit,
            last_self_eval=last_self_eval,
        )
        progress = self.tracker.record_exhausted_attempt(progress, feature.id, branch)
        if reverted:
            try:
                commit_all(
                    project_dir,
                    f"chore(harness): record exhausted attempt {attempt_number} of {feature.id}",
                )
            except (subprocess.CalledProcessError, OSError):
                pass

        lines = [f"[yellow]{feature.name}[/yellow] ({feature.id}) exhausted its iteration budget."]
        if branch:
            lines.append(f"Attempt preserved on branch [bold]{branch}[/bold].")
        if reverted:
            lines.append(f"Working branch reverted to {base[:10]} — the next feature starts clean.")
        lines.append(f"Notes for the next attempt: {notes.relative_to(project_dir).as_posix()}")
        console.print(Panel("\n".join(lines), title="[yellow]Feature exhausted[/yellow]", border_style="yellow"))
        return progress

    def _state_files(self) -> list[Path]:
        """Harness-owned files that must survive a `git reset --hard`."""
        project_dir = Path(self.config.output_dir)
        names = {
            self.config.features_file,
            self.config.progress_file,
            self.config.spec_file,
            CONFIG_FILENAME,
        }
        files = [project_dir / name for name in sorted(names)]
        files += sorted(project_dir.glob("handoff_session_*.json"))
        harness_dir = project_dir / ".harness"
        if harness_dir.is_dir():
            files += sorted(p for p in harness_dir.rglob("*") if p.is_file())
        return [p for p in files if p.is_file()]

    def _snapshot_state_files(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self._state_files()}

    @staticmethod
    def _restore_state_files(snapshot: dict[Path, bytes]) -> None:
        for path, data in snapshot.items():
            write_atomic(path, data)

    @staticmethod
    def _timeout_result(exc: RunnerTimeoutError, iteration: int) -> EvaluationResult:
        """A failed, ungraded iteration for a generator session that timed out."""
        limit = f" after {exc.timeout_seconds}s" if exc.timeout_seconds else ""
        return EvaluationResult(
            design_quality=0.0,
            originality=0.0,
            craft=0.0,
            functionality=0.0,
            overall_score=0.0,
            feedback=(
                f"The implementation session timed out{limit} and was not evaluated. "
                "Work in smaller steps: get the core of the feature working first, "
                "commit, then refine. Avoid long-running or interactive commands "
                "(dev servers, watchers) that never exit."
            ),
            passed=False,
            iteration=iteration,
            failure_reason="generator_timeout",
        )

    def _reload_config_if_changed(self) -> dict[str, tuple]:
        """Re-read harness_config.json and apply mutable field changes in place.

        Agents read self.config.<role>_model as a live property each call,
        so mutating self.config is enough — no need to reconstruct agents.

        Returns {field: (old, new)} for fields that actually changed (empty
        if nothing changed). Identity fields like project_id/output_dir/
        code_runner are never touched.
        """
        if not self._config_path.exists():
            return {}
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return {}
        if mtime <= self._config_mtime:
            return {}
        self._config_mtime = mtime

        try:
            fresh = HarnessConfig.from_yaml(self._config_path)
        except Exception as e:
            console.print(
                f"[yellow]{CONFIG_FILENAME} changed but failed to parse ({e}); "
                "keeping current values.[/yellow]"
            )
            return {}

        changes: dict[str, tuple] = {}
        for field in self._LIVE_RELOAD_FIELDS:
            old = getattr(self.config, field)
            new = getattr(fresh, field)
            if old != new:
                setattr(self.config, field, new)
                changes[field] = (old, new)

        if changes:
            if any(
                field in changes for field in (
                    "runner_profiles",
                    "planner_runner_order",
                    "generator_runner_order",
                    "evaluator_runner_order",
                    "reviewer_runner_order",
                )
            ):
                self._role_profile_index.clear()
                self._role_runner_cache.clear()
            lines = [f"[cyan]{CONFIG_FILENAME} changed — applying live:[/cyan]"]
            for field, (old, new) in changes.items():
                lines.append(f"  [dim]{field}:[/dim] {old} → [bold]{new}[/bold]")
            console.print("\n".join(lines))
        return changes

    def _with_role_fallback(self, role: str, call, protect_state: bool = False):
        """Run one role call, rotating profiles when a runner reports a cap.

        protect_state=True restores features.json if the agent session
        modified it (C-10). The initializer is the one role allowed to write
        it, so it does not pass the flag.
        """
        while True:
            runner = self._agent_runner(role)
            try:
                if protect_state:
                    with self.tracker.protect_state():
                        return call(runner)
                return call(runner)
            except RunnerRateLimitedError as exc:
                if self._advance_role_runner(role, exc):
                    continue
                raise

    def _runner_for_role(self, role: str):
        profiles = role_profiles(self.config, role)
        if not profiles:
            return self.runner

        index = min(self._role_profile_index.get(role, 0), len(profiles) - 1)
        self._role_profile_index[role] = index
        key = (role, index)
        if key not in self._role_runner_cache:
            self._role_runner_cache[key] = runner_for_profile(self.config, profiles[index])
        return self._role_runner_cache[key]

    def _advance_role_runner(self, role: str, exc: RunnerRateLimitedError) -> bool:
        if not self.config.fallback_on_rate_limit or not self.config.runner_profiles:
            return False

        profiles = role_profiles(self.config, role)
        current = self._role_profile_index.get(role, 0)
        next_index = current + 1
        if next_index >= len(profiles):
            return False

        old = profiles[current]
        new = profiles[next_index]
        self._role_profile_index[role] = next_index
        reset = f" Resets at {exc.reset_at.astimezone().strftime('%Y-%m-%d %H:%M %Z')}." if exc.reset_at else ""
        console.print(
            Panel(
                f"[yellow]{role} runner capped:[/yellow] {old.name} ({old.runner}).{reset}\n"
                f"Switching to [bold]{new.name}[/bold] ({new.runner}).",
                title="[cyan]Runner fallback[/cyan]",
                border_style="cyan",
            )
        )
        return True

    def _sync_project_git(self, reason: str) -> None:
        try:
            result = sync_project_git(self.config, reason=reason)
        except ProjectGitConfigurationError as exc:
            # Configuration errors are not graceful-degrade material —
            # `harness setup` is supposed to keep this from happening.
            # Print a red panel and re-raise so the run aborts cleanly
            # rather than churning through features with broken sync.
            console.print(
                Panel(
                    str(exc),
                    title="[red]Project GitHub sync misconfigured[/red]",
                    border_style="red",
                )
            )
            raise
        if result.skipped:
            return
        if result.ok:
            console.print(f"  [green]GitHub sync:[/green] {result.message}")
            return
        # Transient runtime issues (network blip, push rejected, etc.)
        # remain a yellow warning — they don't kill the run.
        console.print(
            Panel(
                result.message,
                title="[yellow]Project GitHub sync failed[/yellow]",
                border_style="yellow",
            )
        )

    def should_reset(self) -> bool:
        return self.context_reset.should_reset(self.total_tokens)

    def _do_context_reset(
        self,
        progress: ProjectProgress,
        current_feature_id: str,
        last_feedback: str | None,
    ) -> HandoffDocument:
        feature = progress.get_feature(current_feature_id)
        self.session_number += 1
        progress.session_count = self.session_number
        self.tracker.save(progress)

        latest_score = (
            f"{feature.latest_evaluation.overall_score:.1f}"
            if feature.latest_evaluation
            else "N/A"
        )
        return self.context_reset.build_handoff(
            progress=progress,
            session_number=self.session_number,
            feature_id=current_feature_id,
            what_was_done=f"Worked on feature '{feature.name}' ({feature.iteration_count} iterations)",
            current_state=(
                f"Feature '{feature.name}' is still failing. Latest score: {latest_score}"
            ),
            next_action=(
                f"Continue implementing feature '{feature.name}' (id: {feature.id}). "
                f"Address the evaluator feedback before retrying."
            ),
            warnings=[last_feedback[:500]] if last_feedback else [],
        )

    def _print_runner_status(self) -> None:
        """Run preflight check and print a clear status banner. Abort on failure."""
        from rich.panel import Panel as RichPanel
        if self.config.runner_profiles:
            lines = ["[bold]Runner rotation enabled[/bold]"]
            for role in ("planner", "generator", "evaluator", "reviewer"):
                profiles = role_profiles(self.config, role)
                order = " → ".join(
                    f"{p.name} ({p.runner}{f'/{p.model}' if p.model else ''})"
                    for p in profiles
                )
                lines.append(f"[dim]{role}:[/dim] {order or 'default runner'}")
            console.print(
                RichPanel(
                    "\n".join(lines),
                    title="[green]✓ Runner policy ready[/green]",
                    border_style="green",
                )
            )
            return

        pf = self.runner.preflight()

        if not pf.ok:
            console.print(
                RichPanel(
                    f"[bold red]Runner error:[/bold red] {self.runner.runner_type.value}\n\n"
                    f"{pf.error}",
                    title="[red]Cannot start[/red]",
                    border_style="red",
                )
            )
            raise SystemExit(1)

        mode = self.config.orchestration_mode
        if mode == "runner":
            mode_label = "[green]runner mode[/green] — planner + evaluator use this runner too (no API key needed)"
        else:
            mode_label = "[yellow]api mode[/yellow] — planner + evaluator use Anthropic API (ANTHROPIC_API_KEY required)"

        lines = [
            f"[bold]{pf.summary}[/bold]",
            f"[dim]{pf.details}[/dim]",
            f"\nOrchestration: {mode_label}",
        ]
        if pf.warning:
            lines.append(f"\n[yellow]⚠  {pf.warning}[/yellow]")

        console.print(
            RichPanel(
                "\n".join(lines),
                title=f"[green]✓ Runner ready[/green]  [dim]({self.runner.runner_type.value})[/dim]",
                border_style="green",
            )
        )

    def _print_eval_summary(self, result: EvaluationResult) -> None:
        w = self.config.evaluator_weights
        console.print(
            f"    Design={result.design_quality:.1f}×{w.design_quality} "
            f"Originality={result.originality:.1f}×{w.originality} "
            f"Craft={result.craft:.1f}×{w.craft} "
            f"Functionality={result.functionality:.1f}×{w.functionality} "
            f"→ [bold]{result.overall_score:.2f}[/bold]/10"
        )
