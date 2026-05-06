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

from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel

from harness.config import HarnessConfig
from harness.agents import InitializerAgent, PlannerAgent, GeneratorAgent, EvaluatorAgent
from harness.runners import create_runner, RunnerType
from harness.runners.base import RunnerRateLimitedError
from harness import auto_resume
from harness.context.reset import ContextReset
from harness.context.handoff import HandoffDocument
from harness.progress.tracker import ProgressTracker
from harness.progress.models import ProjectProgress, EvaluationResult
from harness.session.opener import SessionOpener

console = Console()


class Orchestrator:
    """Drives the full harness lifecycle for a project."""

    # Fields that are safe to live-reload from config.yaml between seams.
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
        "context_reset_threshold_tokens",
        "sprint_contract_enabled",
        "code_runner_model",
        "codex_oss",
        "codex_local_provider",
        "code_runner_extra_args",
        "progress_animation",
        "progress_phrase_style",
        "progress_text_effect",
    )

    def __init__(self, config: HarnessConfig, runner_type: RunnerType | None = None) -> None:
        self.config = config
        self.tracker = ProgressTracker(config)
        self.context_reset = ContextReset(config)
        self.session_opener = SessionOpener(config)
        self.total_tokens = 0
        self.session_number = 1
        # Track config.yaml mtime so we can pick up edits between seams.
        self._config_path = Path(config.output_dir) / "config.yaml"
        self._config_mtime = (
            self._config_path.stat().st_mtime if self._config_path.exists() else 0.0
        )
        # Resolve runner: explicit arg > config value > default (subprocess)
        _rt = runner_type or RunnerType(config.code_runner or "subprocess")
        self.runner = create_runner(_rt, config)
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

        try:
            # Phase 1: Initialize (idempotent — skips if already done)
            progress = self._initialize()

            # Phase 2: Plan (skip if a confirmed spec was supplied)
            progress = self._plan(progress, confirmed_spec=confirmed_spec)

            # Phase 3: Feature implementation loop
            self._feature_loop(progress)
        except RunnerRateLimitedError as exc:
            self._handle_rate_limit(exc)
            return

        # Reload final state from disk — _feature_loop mutates progress internally
        final = self.tracker.load()
        console.print(
            Panel(
                f"[bold green]Done![/bold green] "
                f"{len(final.passing_features)}/{len(final.features)} features passing.",
                title="Harness Complete",
            )
        )

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

        reviewer = ReviewerAgent(self.config, runner=self._agent_runner())
        reviewer.review(progress=progress)

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
        local_reset = exc.reset_at.astimezone()
        wait = exc.reset_at - datetime.now(timezone.utc)
        hours, rem = divmod(int(wait.total_seconds()), 3600)
        minutes = rem // 60
        wait_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

        body_lines = [
            "[yellow]Subscription usage cap reached.[/yellow]",
            f"  Resets at [bold]{local_reset.strftime('%Y-%m-%d %H:%M %Z')}[/bold] "
            f"(in [bold]{wait_str}[/bold])",
            "",
        ]

        scheduled = None
        if self.config.auto_resume_on_rate_limit:
            try:
                scheduled = auto_resume.schedule(
                    project_dir=Path(self.config.output_dir),
                    project_id=self.config.project_id,
                    fire_at_utc=exc.reset_at,
                )
            except Exception as e:  # scheduling is best-effort
                body_lines.append(f"[red]Could not schedule auto-resume:[/red] {e}")

        if scheduled:
            body_lines.append(
                f"[green]Auto-resume scheduled[/green] for "
                f"[bold]{scheduled['fire_local'].strftime('%H:%M %Z')}[/bold] "
                f"via launchd label [dim]{scheduled['label']}[/dim]"
            )
            body_lines.append(f"  Log: {scheduled['log']}")
            body_lines.append(
                f"  Cancel: [dim]launchctl bootout gui/$(id -u)/{scheduled['label']}[/dim]"
            )
        else:
            body_lines.append(
                f"To continue manually after reset, run:\n"
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

    def _agent_runner(self):
        """Return the runner to pass to orchestration agents (planner/evaluator/initializer).

        In 'runner' mode all agents share the same runner.
        In 'api' mode they get None and use the Anthropic API directly.
        """
        return self.runner if self.config.orchestration_mode == "runner" else None

    def _initialize(self) -> ProjectProgress:
        console.print("\n[bold blue]Phase 1: Initialize[/bold blue]")
        agent = InitializerAgent(self.config, runner=self._agent_runner())
        return agent.run(brief=self.config.brief)

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

        agent = PlannerAgent(self.config, runner=self._agent_runner())
        spec = agent.run(brief=self.config.brief)
        progress.spec = spec
        self.tracker.save(progress)
        self._account_tokens(agent.usage.total_tokens)
        return progress

    def _feature_loop(self, progress: ProjectProgress) -> None:
        console.print("\n[bold blue]Phase 3: Feature implementation loop[/bold blue]")

        generator = GeneratorAgent(self.config, runner=self.runner)
        evaluator = EvaluatorAgent(self.config, runner=self._agent_runner())

        handoff = HandoffDocument.load_latest(Path(self.config.output_dir))

        while True:
            # Seam 1: between features — pick up any config edits before
            # negotiating the next sprint contract.
            self._reload_config_if_changed()

            feature = progress.next_pending_feature()
            if not feature:
                console.print("[green]All features are passing. Harness complete.[/green]")
                break

            console.print(f"\n[bold]Feature:[/bold] {feature.name} [{feature.id}]")

            # Mark in progress
            progress = self.tracker.mark_in_progress(progress, feature.id)

            # Build session opening context
            session_ctx = self.session_opener.build_opening_context(
                progress=progress,
                handoff=handoff,
                include_git_log=True,
            )

            # Negotiate sprint contract (once per feature)
            if self.config.sprint_contract_enabled and feature.sprint_contract is None:
                console.print("  Negotiating sprint contract...")
                contract = generator.negotiate_sprint_contract(
                    feature=feature,
                    spec=progress.spec or self.config.brief,
                )
                progress = self.tracker.attach_sprint_contract(progress, feature.id, contract)
                feature = progress.get_feature(feature.id)  # refresh

            # GAN-style generator ↔ evaluator loop.
            # Use a while-loop (not range) so live edits to
            # max_iterations_per_feature take effect on the next iteration.
            evaluator_feedback: str | None = None
            passed = False
            iteration = 0

            while iteration < self.config.max_iterations_per_feature:
                # Seam 2: between iterations within a feature — model swaps,
                # threshold changes, etc. take effect from the next call.
                self._reload_config_if_changed()
                iteration += 1
                console.print(f"  [cyan]Iteration {iteration}[/cyan]")

                # Generate
                self_eval = generator.implement_feature(
                    feature=feature,
                    progress=progress,
                    session_preamble=session_ctx,
                    evaluator_feedback=evaluator_feedback,
                    iteration=iteration,
                )
                self._account_tokens(generator.usage.total_tokens)

                # Evaluate
                result: EvaluationResult = evaluator.evaluate(
                    feature=feature,
                    generator_self_eval=self_eval,
                    iteration=iteration,
                )
                self._account_tokens(evaluator.usage.total_tokens)

                self._print_eval_summary(result)

                # Record
                progress = self.tracker.record_evaluation(progress, feature.id, result)
                feature = progress.get_feature(feature.id)

                if result.passed:
                    console.print(f"  [green]✓ Passed (score {result.overall_score:.1f})[/green]")
                    passed = True
                    break

                evaluator_feedback = result.feedback
                console.print(
                    f"  [yellow]✗ Score {result.overall_score:.1f} < {self.config.evaluator_pass_score} — iterating[/yellow]"
                )

                # Check if we need a context reset
                if self.should_reset():
                    console.print("  [red]Context budget exceeded — triggering reset[/red]")
                    handoff = self._do_context_reset(progress, feature.id, evaluator_feedback)
                    session_ctx = self.session_opener.build_opening_context(
                        progress=progress,
                        handoff=handoff,
                        include_git_log=True,
                    )
                    self.total_tokens = 0

            if not passed:
                console.print(
                    f"  [red]Feature {feature.name} did not pass after "
                    f"{self.config.max_iterations_per_feature} iterations.[/red]"
                )

            progress = self.tracker.load()  # reload from disk

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _account_tokens(self, tokens: int) -> None:
        self.total_tokens += tokens

    def _reload_config_if_changed(self) -> dict[str, tuple]:
        """Re-read config.yaml and apply mutable field changes in place.

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
                f"[yellow]config.yaml changed but failed to parse ({e}); "
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
            lines = ["[cyan]config.yaml changed — applying live:[/cyan]"]
            for field, (old, new) in changes.items():
                lines.append(f"  [dim]{field}:[/dim] {old} → [bold]{new}[/bold]")
            console.print("\n".join(lines))
        return changes

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
