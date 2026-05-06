"""CLI entry point for the Agent Harness."""

import re
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from pathlib import Path

from harness.config import CONFIG_FILENAME, HarnessConfig, RunnerProfile
from harness.orchestrator import Orchestrator
from harness.progress.tracker import ProgressTracker
from harness.runner_profiles import (
    HarnessSetup,
    apply_setup_defaults,
    default_runner_from_setup,
    default_setup_path,
    load_setup,
    runner_for_profile,
    role_profiles,
    save_setup,
)
from harness.runners.base import RunnerType

console = Console()


# ── Runner selection ──────────────────────────────────────────────────────────

_RUNNER_TABLE = [
    # (value, family, billing, file_i/o, requires)
    # All runners are agentic. API providers (Anthropic, OpenAI, Gemini,
    # OpenRouter) plug into one of these via env vars; they are not
    # standalone runners. See harness/runners/base.py for the contract.
    ("subprocess", "Agentic", "Claude subscription", "✅ full", "`claude` CLI installed"),
    ("sdk",        "Agentic", "Claude subscription", "✅ full", "pip install claude-code-sdk"),
    ("codex",      "Agentic", "OpenAI subscription", "✅ full", "`codex` CLI installed"),
]


def _print_runner_menu() -> None:
    table = Table(title="Available Runners", show_header=True, header_style="bold cyan")
    table.add_column("Runner",     style="bold")
    table.add_column("Family",     style="dim")
    table.add_column("Billing")
    table.add_column("File I/O")
    table.add_column("Requires",   style="dim")

    for row in _RUNNER_TABLE:
        table.add_row(*row)

    console.print()
    console.print(table)
    console.print()


def _prompt_runner() -> RunnerType:
    """Interactively ask the user which runner to use."""
    _print_runner_menu()
    choices = [r[0] for r in _RUNNER_TABLE]
    value = click.prompt(
        "Choose a runner",
        type=click.Choice(choices, case_sensitive=False),
        default="subprocess",
    )
    return RunnerType(value)


def _agentic_runner_values() -> list[str]:
    """Runner values that can edit files through a signed-in coding agent."""
    return [runner.value for runner in RunnerType.agentic()]


def _print_agentic_runner_menu() -> None:
    table = Table(title="Agentic Runners", show_header=True, header_style="bold cyan")
    table.add_column("Runner", style="bold")
    table.add_column("Billing")
    table.add_column("Requires", style="dim")

    agentic = set(_agentic_runner_values())
    for value, _family, billing, _file_io, requires in _RUNNER_TABLE:
        if value in agentic:
            table.add_row(value, billing, requires)

    console.print()
    console.print(table)
    console.print()


def _prompt_agentic_runner() -> RunnerType:
    """Interactively ask for a runner that can write the theme into the code."""
    _print_agentic_runner_menu()
    value = click.prompt(
        "Choose the signed-in agent to edit the theme",
        type=click.Choice(_agentic_runner_values(), case_sensitive=False),
        default=RunnerType.SUBPROCESS.value,
    )
    return RunnerType(value)


def _resolve_runner(config: HarnessConfig, runner_flag: str | None) -> RunnerType:
    """Resolve runner with priority: CLI flag > config file > interactive prompt."""
    if runner_flag:
        return RunnerType(runner_flag)
    if config.code_runner:
        console.print(f"[dim]Using runner from config: {config.code_runner}[/dim]")
        return RunnerType(config.code_runner)
    return _prompt_runner()


# Named-flag → RunnerType mapping (for `harness new --claude-code` etc.)
# Only the three coding-agent runners exist; API providers plug in via env.
_FLAG_TO_RUNNER: dict[str, RunnerType] = {
    "claude_code":   RunnerType.SUBPROCESS,
    "claude_sdk":    RunnerType.SDK,
    "codex":         RunnerType.CODEX,
}


def _pick_runner_from_flags(**flags: bool) -> RunnerType | None:
    """Return a RunnerType if exactly one named flag is set, error if multiple."""
    chosen = [rt for flag, rt in _FLAG_TO_RUNNER.items() if flags.get(flag)]
    if len(chosen) > 1:
        names = ", ".join(f"--{f.replace('_', '-')}" for f, rt in _FLAG_TO_RUNNER.items()
                          if rt in chosen)
        raise click.UsageError(f"Only one runner flag allowed at a time. Got: {names}")
    return chosen[0] if chosen else None


def _default_orchestration_mode(runner_type: RunnerType) -> str:
    """All current runners default to 'runner' mode (no API key needed).

    Users can still flip a project's config to orchestration_mode='api' to
    have planner/evaluator/initializer call the Anthropic API directly while
    the generator uses the runner — that path requires ANTHROPIC_API_KEY.
    """
    return "runner"


def _model_prompt_hint(runner_type: RunnerType) -> str:
    """Short hint for the coding-agent model prompt."""
    if runner_type in (RunnerType.SUBPROCESS, RunnerType.SDK):
        return "Claude Code model alias or full ID, e.g. sonnet, opus, claude-sonnet-4-6"
    if runner_type == RunnerType.CODEX:
        return "Codex model ID, e.g. gpt-5.2, gpt-5.4, or a local OSS model"
    return "Coding agent model ID"


def _resolve_model_choice(runner_type: RunnerType, model_flag: str | None) -> str | None:
    """Resolve the coding model for a new project.

    Empty means "let the selected coding agent use its configured default".
    """
    if model_flag is not None:
        model = model_flag.strip()
        return model or None

    console.print(
        "\n[bold]Coding agent model[/bold]\n"
        "[dim]This is the engine inside the Claude Code/Codex agent frame. "
        "Press Enter to use the runner's default.[/dim]"
    )
    console.print(f"[dim]{_model_prompt_hint(runner_type)}[/dim]")
    model = console.input("Model: ").strip()
    return model or None


def _apply_model_override(config: HarnessConfig, runner_type: RunnerType, model: str | None) -> None:
    """Apply a CLI model override to the right config field."""
    if not model:
        return
    if runner_type in RunnerType.agentic():
        config.code_runner_model = model
    else:
        config.generator_model = model


def _normalize_project_git_inputs(
    github_repo: str | None,
    git_remote: str | None,
) -> tuple[str | None, str | None]:
    if github_repo and git_remote:
        raise click.UsageError("Use either --github-repo or --git-remote, not both.")
    if github_repo:
        repo = github_repo.strip()
        if "://" in repo or repo.startswith("git@"):
            raise click.UsageError(
                "Use --github-repo owner/repo, or pass the full URL to --git-remote."
            )
        if repo.count("/") != 1 or any(not part.strip() for part in repo.split("/")):
            raise click.UsageError("--github-repo must use owner/repo format.")
        github_repo = repo
    if git_remote:
        git_remote = git_remote.strip()
    return github_repo, git_remote


def _apply_project_git_options(
    config: HarnessConfig,
    github_repo: str | None,
    git_remote: str | None,
    github_private: bool,
    no_git_push: bool,
) -> None:
    """Persist per-output-project GitHub settings."""
    github_repo, git_remote = _normalize_project_git_inputs(github_repo, git_remote)
    if github_repo:
        config.project_github_repo = github_repo
        config.project_github_private = github_private
    if git_remote:
        config.project_git_remote = git_remote
    if github_repo or git_remote:
        config.project_git_push = not no_git_push


def _print_project_git_sync_result(result) -> None:
    if result.skipped:
        return
    if result.ok:
        console.print(f"  [green]GitHub sync:[/green] {result.message}")
        return
    console.print(
        Panel(
            result.message,
            title="[yellow]Project GitHub sync failed[/yellow]",
            border_style="yellow",
        )
    )


def _apply_saved_setup(config: HarnessConfig) -> None:
    setup = load_setup()
    if setup and apply_setup_defaults(config, setup):
        console.print(f"[dim]Loaded runner setup from {default_setup_path()}[/dim]")


def _resolve_new_runner(
    named: RunnerType | None,
    runner_flag: str | None,
    setup: HarnessSetup | None,
) -> RunnerType:
    if named:
        return named
    if runner_flag:
        return RunnerType(runner_flag)
    setup_runner = default_runner_from_setup(setup)
    if setup_runner:
        console.print(f"[dim]Using runner from setup: {setup_runner.value}[/dim]")
        return setup_runner
    return _prompt_runner()


def _parse_profile(value: str) -> RunnerProfile:
    parts = value.split(":")
    if len(parts) < 2:
        raise click.UsageError("--profile must use name:runner[:model[:provider]]")
    name, runner = parts[0].strip(), parts[1].strip()
    if not name:
        raise click.UsageError("Profile name cannot be empty.")
    model = parts[2].strip() or None if len(parts) >= 3 else None
    provider = parts[3].strip() if len(parts) >= 4 and parts[3].strip() else "subscription"
    if runner not in RunnerType.choices():
        raise click.UsageError(f"Unknown runner in profile {name!r}: {runner}")
    try:
        return RunnerProfile(name=name, runner=runner, model=model, provider=provider)
    except Exception as exc:
        raise click.UsageError(f"Invalid profile {name!r}: {exc}") from exc


def _parse_csv_order(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _build_setup_from_options(
    profiles: tuple[str, ...],
    profile_envs: tuple[str, ...],
    profile_extra_args: tuple[str, ...],
    planner_order: str | None,
    generator_order: str | None,
    evaluator_order: str | None,
    reviewer_order: str | None,
    fallback_on_rate_limit: bool,
) -> HarnessSetup:
    parsed = [_parse_profile(value) for value in profiles]
    by_name = {profile.name: profile for profile in parsed}
    if len(by_name) != len(parsed):
        raise click.UsageError("Profile names must be unique.")

    for item in profile_envs:
        if ":" not in item or "=" not in item:
            raise click.UsageError("--profile-env must use profile:KEY=VALUE")
        profile_name, assignment = item.split(":", 1)
        key, value = assignment.split("=", 1)
        if profile_name not in by_name:
            raise click.UsageError(f"Unknown profile for --profile-env: {profile_name}")
        by_name[profile_name].env[key] = value

    for item in profile_extra_args:
        if ":" not in item:
            raise click.UsageError("--profile-extra-arg must use profile:ARG")
        profile_name, arg = item.split(":", 1)
        if profile_name not in by_name:
            raise click.UsageError(f"Unknown profile for --profile-extra-arg: {profile_name}")
        by_name[profile_name].extra_args.append(arg)

    names = [profile.name for profile in parsed]
    generator = _parse_csv_order(generator_order) or names
    planner = _parse_csv_order(planner_order) or generator
    evaluator = _parse_csv_order(evaluator_order) or generator
    reviewer = _parse_csv_order(reviewer_order) or evaluator

    known = set(names)
    for label, order in {
        "planner": planner,
        "generator": generator,
        "evaluator": evaluator,
        "reviewer": reviewer,
    }.items():
        missing = [name for name in order if name not in known]
        if missing:
            raise click.UsageError(f"{label} order references unknown profiles: {', '.join(missing)}")

    return HarnessSetup(
        runner_profiles=parsed,
        planner_runner_order=planner,
        generator_runner_order=generator,
        evaluator_runner_order=evaluator,
        reviewer_runner_order=reviewer,
        fallback_on_rate_limit=fallback_on_rate_limit,
    )


def _interactive_setup() -> HarnessSetup:
    console.print(
        Panel(
            "Define the priority order Harness should use when runners hit usage caps.",
            title="[blue]Harness Setup[/blue]",
            border_style="blue",
        )
    )
    order = click.prompt(
        "Default runner order",
        default="claude,codex",
    )
    names = _parse_csv_order(order)
    profiles: list[RunnerProfile] = []
    for name in names:
        if name in {"claude", "claude-code"}:
            model = click.prompt("Claude Code model", default="", show_default=False).strip() or None
            profiles.append(RunnerProfile(name="claude", runner="subprocess", model=model))
        elif name == "sdk":
            model = click.prompt("Claude SDK model", default="", show_default=False).strip() or None
            profiles.append(RunnerProfile(name="sdk", runner="sdk", model=model))
        elif name == "codex":
            model = click.prompt("Codex model", default="", show_default=False).strip() or None
            profiles.append(RunnerProfile(name="codex", runner="codex", model=model))
        else:
            runner_value = click.prompt(
                f"Runner for profile {name}",
                type=click.Choice(RunnerType.choices(), case_sensitive=False),
                default="subprocess",
            )
            model = click.prompt(f"Model for {name}", default="", show_default=False).strip() or None
            profiles.append(RunnerProfile(name=name, runner=runner_value, model=model))

    if click.confirm("Add Claude Code via OpenRouter as an API fallback?", default=False):
        model = click.prompt(
            "OpenRouter model",
            default="anthropic/claude-sonnet-4-6",
        )
        profiles.append(RunnerProfile(
            name="claude-openrouter",
            runner="subprocess",
            model=model,
            provider="openrouter",
            env={
                "ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1",
                "ANTHROPIC_AUTH_TOKEN": "$OPENROUTER_API_KEY",
            },
        ))
        names.append("claude-openrouter")

    default_order = ",".join(names)
    planner = _parse_csv_order(click.prompt("Planner order", default=default_order))
    generator = _parse_csv_order(click.prompt("Coding/generator order", default=default_order))
    evaluator = _parse_csv_order(click.prompt("Evaluator order", default=default_order))
    reviewer = _parse_csv_order(click.prompt("Reviewer order", default=",".join(evaluator)))
    return HarnessSetup(
        runner_profiles=profiles,
        planner_runner_order=planner,
        generator_runner_order=generator,
        evaluator_runner_order=evaluator,
        reviewer_runner_order=reviewer,
        fallback_on_rate_limit=True,
    )


def _print_setup(setup: HarnessSetup, path: Path) -> None:
    table = Table(title=f"Harness Setup: {path}", show_header=True, header_style="bold cyan")
    table.add_column("Profile", style="bold")
    table.add_column("Runner")
    table.add_column("Model")
    table.add_column("Provider")
    table.add_column("Env", style="dim")
    for profile in setup.runner_profiles:
        table.add_row(
            profile.name,
            profile.runner,
            profile.model or "default",
            profile.provider,
            ", ".join(profile.env.keys()) or "-",
        )
    console.print(table)
    console.print(
        f"[dim]planner:[/dim] {', '.join(setup.planner_runner_order)}\n"
        f"[dim]generator:[/dim] {', '.join(setup.generator_runner_order)}\n"
        f"[dim]evaluator:[/dim] {', '.join(setup.evaluator_runner_order)}\n"
        f"[dim]reviewer:[/dim] {', '.join(setup.reviewer_runner_order)}"
    )


def _require_anthropic_key_for_api_mode(orchestration_mode: str) -> None:
    """Abort with a clear message if API orchestration mode needs a key that isn't set."""
    import os
    if orchestration_mode != "api":
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            Panel(
                "[bold red]ANTHROPIC_API_KEY is not set.[/bold red]\n\n"
                "You are using [bold]api orchestration mode[/bold], which routes the\n"
                "planner and evaluator through the Anthropic API.\n\n"
                "Fix:\n"
                "  [bold]export ANTHROPIC_API_KEY=sk-ant-...[/bold]\n\n"
                "Or use a subscription runner without [dim]--with-api[/dim] to run\n"
                "everything through your Claude/Codex subscription instead.\n\n"
                "Get a key at: https://console.anthropic.com/settings/keys",
                title="[red]Missing API key[/red]",
                border_style="red",
            )
        )
        raise SystemExit(1)


def _harness_source_root() -> Path:
    """Find the local Harness checkout that the theme agent should edit."""
    cli_root = Path(__file__).resolve().parent
    candidates = [cli_root, Path.cwd().resolve()]
    for root in candidates:
        if (root / "harness" / "ui" / "spinner.py").exists():
            return root
    raise click.UsageError(
        "Could not find harness/ui/spinner.py. Run this command from the "
        "Harness source checkout."
    )


def _load_animation_theme_guide(root: Path) -> str:
    guide_path = root / "docs" / "technical" / "animation_theme_agent_guide.md"
    if guide_path.exists():
        return guide_path.read_text()
    return (
        "# Animation Theme Agent Guide\n\n"
        "Edit harness/ui/spinner.py, specifically PHRASES[\"playful\"]. "
        "Use short single Title Case verbs only. Keep PHRASES[\"steady\"] "
        "unchanged. Do not add suffixes like 'with Claude Code' or full phrases. "
        "Run python -m pytest tests/test_spinner.py."
    )


def _build_animation_theme_prompt(theme: str, guide: str) -> str:
    return f"""You are editing the Agent Harness terminal animation theme.

User-requested theme:
{theme}

Your task:
- Update the playful quiet-animation verb pools in `harness/ui/spinner.py`.
- Edit only `PHRASES["playful"]` unless a test has to be adjusted.
- Generate tasteful, restrained words that match the theme.
- Keep every entry a single Title Case verb or gerund, like `Scrying` or `Inscribing`.
- Use 4-8 words per phase: `planning`, `coding`, `evaluating`, and `waiting`.
- Do not add prefixes, suffixes, agent names, subjects, slogans, or phrases.
- Do not use words like `Cooking`, `Vibing`, memes, or loud joke wording.
- Do not change frame packs, spinner mechanics, user planning notes, generated output, logs, or cache files.
- Run `python -m pytest tests/test_spinner.py`.
- Do not commit. Leave a concise final summary with files changed, the interpreted theme, and tests run.

Reference guide:

{guide}
"""


# ── Commands ──────────────────────────────────────────────────────────────────

@click.group()
def main():
    """Agent Harness — long-running multi-session agent orchestration."""


def _slugify(name: str) -> str:
    """Convert a project name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:40]  # cap length


# ── harness new ───────────────────────────────────────────────────────────────

def _runner_flags(f):
    """Decorator that attaches all named runner flags to a Click command."""
    flags = [
        ("--claude-code",   "claude_code",   "Claude Code CLI   (subprocess, uses your Claude subscription)"),
        ("--claude-sdk",    "claude_sdk",    "Claude Code SDK   (uses your Claude subscription, structured output)"),
        ("--codex",         "codex",         "OpenAI Codex CLI  (uses your OpenAI subscription)"),
    ]
    for flag, param, help_text in reversed(flags):
        f = click.option(flag, param, is_flag=True, default=False, help=help_text)(f)
    return f


@main.command()
@click.option("--config", "config_path", type=click.Path(), default=None,
              help="Setup file to read/write. Defaults to ~/.harness/setup.json.")
@click.option("--profile", "profiles", multiple=True,
              help="Runner profile as name:runner[:model[:provider]]. Can be repeated.")
@click.option("--profile-env", "profile_envs", multiple=True,
              help="Per-profile env as profile:KEY=VALUE. Values like $OPENROUTER_API_KEY are expanded at runtime.")
@click.option("--profile-extra-arg", "profile_extra_args", multiple=True,
              help="Extra CLI arg for a profile as profile:ARG. Can be repeated.")
@click.option("--planner-order", default=None, help="Comma-separated profile order for planning.")
@click.option("--generator-order", default=None, help="Comma-separated profile order for coding.")
@click.option("--evaluator-order", default=None, help="Comma-separated profile order for evaluation.")
@click.option("--reviewer-order", default=None, help="Comma-separated profile order for review.")
@click.option("--fallback-on-rate-limit/--no-fallback-on-rate-limit", default=True,
              help="Rotate to the next profile when the active runner reports a usage cap.")
@click.option("--show", is_flag=True, default=False, help="Show the saved setup.")
@click.option("--auto-install/--no-auto-install", default=False,
              help="Try to auto-install missing tools (`brew install gh` on macOS, "
                   "`winget install GitHub.cli` on Windows). Off by default.")
@click.option("--skip-preflight", is_flag=True, default=False,
              help="Skip the gh + coding-agent CLI preflight. Use only when you "
                   "know what you're doing.")
def setup(
    config_path: str | None,
    profiles: tuple[str, ...],
    profile_envs: tuple[str, ...],
    profile_extra_args: tuple[str, ...],
    planner_order: str | None,
    generator_order: str | None,
    evaluator_order: str | None,
    reviewer_order: str | None,
    fallback_on_rate_limit: bool,
    show: bool,
    auto_install: bool,
    skip_preflight: bool,
):
    """Configure first-run runner rotation and fallback policy.

    GitHub sync is treated as core infrastructure: this command verifies
    that `gh` is installed and authenticated, plus the coding-agent CLI
    for each runner you've configured. Setup is blocked when any required
    tool is missing — pass --auto-install to attempt installation via the
    host's package manager (Homebrew on macOS, winget on Windows).
    """
    path = Path(config_path).expanduser() if config_path else default_setup_path()

    has_noninteractive_input = bool(
        profiles or profile_envs or profile_extra_args
        or planner_order or generator_order or evaluator_order or reviewer_order
    )
    if show and not has_noninteractive_input:
        saved = load_setup(path)
        if saved is None:
            console.print(f"[yellow]No setup file found at {path}.[/yellow]")
            return
        _print_setup(saved, path)
        return

    if has_noninteractive_input:
        if not profiles:
            raise click.UsageError("--profile is required when using non-interactive setup options.")
        setup_config = _build_setup_from_options(
            profiles=profiles,
            profile_envs=profile_envs,
            profile_extra_args=profile_extra_args,
            planner_order=planner_order,
            generator_order=generator_order,
            evaluator_order=evaluator_order,
            reviewer_order=reviewer_order,
            fallback_on_rate_limit=fallback_on_rate_limit,
        )
    else:
        setup_config = _interactive_setup()

    if not skip_preflight:
        if not _run_setup_preflight(setup_config, auto_install=auto_install):
            console.print(
                "[red]Setup not saved.[/red] Fix the missing tools above and "
                "re-run, or pass --skip-preflight to bypass (not recommended)."
            )
            raise SystemExit(1)

    saved_path = save_setup(setup_config, path)
    console.print(f"[green]Harness setup saved:[/green] {saved_path}")
    _print_setup(setup_config, saved_path)


def _run_setup_preflight(setup_config, *, auto_install: bool) -> bool:
    """Verify required tools are present + authenticated. Returns True iff OK."""
    from harness import preflight

    runner_names = {p.runner for p in setup_config.runner_profiles}
    checks = [preflight.gh_check()] + preflight.runner_checks_for(runner_names)

    console.print("\n[bold]Verifying required tools…[/bold]")
    results = preflight.run_preflight(checks, auto_install=auto_install)

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Tool", style="bold")
    table.add_column("Purpose")
    table.add_column("Status")
    for r in results:
        if r.ok:
            status = "[green]ok[/green]"
            if r.installed_now:
                status += " [dim](installed during setup)[/dim]"
        else:
            status = f"[red]{r.error}[/red]"
        table.add_row(r.tool.name, r.tool.purpose, status)
    console.print(table)

    failures = [r for r in results if not r.ok and r.tool.required]
    if not failures:
        console.print("[green]All required tools present.[/green]\n")
        return True

    console.print()
    for r in failures:
        hint = preflight.manual_hint_for(r.tool)
        console.print(Panel(
            f"[red]{r.error}[/red]\n"
            + (f"[dim]{r.auth_detail}[/dim]\n\n" if r.auth_detail else "\n")
            + f"[bold]Install:[/bold]\n{hint}\n\n"
            + (
                "[bold]Then authenticate:[/bold]\n  gh auth login"
                if r.tool.name == "gh" else ""
            ),
            title=f"[red]Missing: {r.tool.name}[/red]",
            border_style="red",
        ))
    return False


@main.command()
@click.option(
    "--runner", "-r",
    type=click.Choice([r[0] for r in _RUNNER_TABLE], case_sensitive=False),
    default=None,
    help="Runner by internal name (advanced). Use the named flags instead.",
)
@click.option(
    "--with-api", "with_api", is_flag=True, default=False,
    help=(
        "Force API orchestration mode: planner + evaluator use the Anthropic API "
        "even when the generator uses a subscription runner. "
        "Requires ANTHROPIC_API_KEY. Default for subscription runners is runner mode "
        "(no API key needed)."
    ),
)
@click.option(
    "--model", "model", default=None,
    help=(
        "Model for the coding agent before the project starts. "
        "Passed as --model to Claude Code / Codex; SDK uses it via ClaudeCodeOptions."
    ),
)
@click.option(
    "--github-repo", "github_repo", default=None,
    help="GitHub repo for this generated project, e.g. owner/repo. Uses gh to create it if needed.",
)
@click.option(
    "--git-remote", "git_remote", default=None,
    help="Existing git remote URL for this generated project.",
)
@click.option(
    "--github-private/--github-public", "github_private", default=True,
    help="Visibility when --github-repo needs to create a repo.",
)
@click.option(
    "--no-git-push", "no_git_push", is_flag=True, default=False,
    help="Store GitHub/remote config but do not push automatically.",
)
@_runner_flags
def new(runner: str | None, with_api: bool, model: str | None,
        github_repo: str | None, git_remote: str | None,
        github_private: bool, no_git_push: bool,
        claude_code: bool, claude_sdk: bool, codex: bool):
    """Create a new project interactively — no config editing needed.

    Pick your runner via a named flag, or leave all flags off to get an
    interactive menu.

    \b
    Coding-agent runners (subscription billing):
      harness new --claude-code     Claude Code CLI
      harness new --claude-sdk      Claude Code SDK
      harness new --codex           OpenAI Codex CLI

    Direct API providers (Anthropic, OpenAI, Gemini, OpenRouter) are not
    standalone runners — set the relevant env var (ANTHROPIC_API_KEY,
    OPENAI_API_KEY, ANTHROPIC_BASE_URL, etc.) and one of the three
    coding-agent runners will use it as its underlying model.
    """
    console.print(
        Panel(
            "[bold]Agent Harness[/bold]\nNew Project Setup",
            subtitle="Let's build something",
            border_style="blue",
        )
    )
    console.print()

    github_repo, git_remote = _normalize_project_git_inputs(github_repo, git_remote)

    # ── Step 1: Resolve runner FIRST so the planner uses the right mode ───
    # Named flags > --runner > interactive prompt. (No config file at this stage.)
    named = _pick_runner_from_flags(
        claude_code=claude_code, claude_sdk=claude_sdk, codex=codex,
    )
    saved_setup = None if (with_api or named or runner) else load_setup()
    runner_type = _resolve_new_runner(named, runner, saved_setup)

    # ── Step 2: Derive orchestration mode and validate keys up front ──────
    # All current runners default to "runner" mode. --with-api opts the
    # planner+evaluator+initializer into the Anthropic API; ANTHROPIC_API_KEY
    # is then required.
    orchestration_mode = "api" if with_api else "runner"
    _require_anthropic_key_for_api_mode(orchestration_mode)

    if model is not None or not (saved_setup and saved_setup.runner_profiles):
        code_model = _resolve_model_choice(runner_type, model)
    else:
        code_model = None

    # ── Step 3: Collect project name and brief ────────────────────────────
    project_name: str = click.prompt("Project name").strip()
    brief: str = click.prompt("What would you like to build? (brief description)").strip()

    # ── Step 4: Generate output directory ─────────────────────────────────
    import uuid
    project_id = uuid.uuid4().hex[:8]
    slug = _slugify(project_name)
    output_dir = Path("./output") / f"{slug}_{project_id}"

    console.print(f"\n[dim]Project ID:[/dim] {project_id}")
    console.print(f"[dim]Output dir:[/dim] {output_dir}\n")

    # ── Step 5: Build config with the resolved runner + orchestration mode ─
    config = HarnessConfig(
        project_name=project_name,
        project_id=project_id,
        brief=brief,
        output_dir=str(output_dir),
        orchestration_mode=orchestration_mode,
        code_runner=runner_type.value,
    )
    if saved_setup:
        apply_setup_defaults(config, saved_setup)
    _apply_model_override(config, runner_type, code_model)
    _apply_project_git_options(config, github_repo, git_remote, github_private, no_git_push)

    # Create the output dir now — the runner-mode planner uses it as cwd.
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 6: In runner mode the planner needs a runner instance ────────
    from harness.runners import create_runner
    planner_runner = None
    if config.orchestration_mode == "runner":
        planner_profiles = role_profiles(config, "planner")
        planner_runner = (
            runner_for_profile(config, planner_profiles[0])
            if planner_profiles else create_runner(runner_type, config)
        )

    # ── Step 7: Requirement alignment with planner ────────────────────────
    from harness.agents.planner import PlannerAgent
    planner = PlannerAgent(config, runner=planner_runner)
    confirmed_spec = planner.align_requirements(brief)

    # ── Step 8: Persist the confirmed spec as project documentation ───────
    # Coding agents reference this in every session via the startup checklist.
    spec_path = config.spec_path
    spec_path.write_text(
        f"# {project_name} — Product Specification\n\n"
        f"_Confirmed during requirement alignment. Treat this as the "
        f"source of truth for what to build._\n\n"
        f"---\n\n{confirmed_spec}\n"
    )
    console.print(f"[dim]Spec saved to {spec_path}[/dim]")

    # ── Step 9: Save config alongside the project ─────────────────────────
    config_path = HarnessConfig.default_config_path(output_dir)
    config.save_yaml(config_path)
    console.print(f"[dim]Config saved to {config_path}[/dim]\n")

    # ── Step 10: Run the full harness (spec injected into progress) ──────
    orchestrator = Orchestrator(config, runner_type=runner_type)
    orchestrator.run(confirmed_spec=confirmed_spec)


@main.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.option(
    "--runner", "-r",
    type=click.Choice([r[0] for r in _RUNNER_TABLE], case_sensitive=False),
    default=None,
    help="Runner to use (skips interactive prompt).",
)
@click.option(
    "--model", "model", default=None,
    help="Override the coding-agent model for this run only.",
)
def run(config_file: str, runner: str | None, model: str | None):
    """Run the full harness for a project defined in CONFIG_FILE."""
    config = HarnessConfig.from_yaml(config_file)
    if runner is None:
        _apply_saved_setup(config)
    _require_anthropic_key_for_api_mode(config.orchestration_mode)

    console.print(
        Panel(
            f"[bold]Agent Harness[/bold]\n{config.project_name}",
            subtitle=config.brief[:80],
        )
    )

    runner_type = _resolve_runner(config, runner)
    if runner is not None:
        config.runner_profiles = []
    _apply_model_override(config, runner_type, model)

    orchestrator = Orchestrator(config, runner_type=runner_type)
    orchestrator.run()


@main.command()
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--runner", "-r",
    type=click.Choice([r[0] for r in _RUNNER_TABLE], case_sensitive=False),
    default=None,
    help=f"Override the runner saved in {CONFIG_FILENAME} (rarely needed).",
)
@click.option(
    "--model", "model", default=None,
    help="Override the coding-agent model for this resume only.",
)
def resume(project_dir: str, runner: str | None, model: str | None):
    """Resume work on an existing project.

    Pass the project's output directory (the one containing harness_config.json).
    The harness picks up wherever it left off:

      • initialize step skips if features.json already exists
      • plan step skips if spec.md already exists
      • feature loop continues from the next pending feature
    """
    project_path = Path(project_dir)
    config_path = HarnessConfig.default_config_path(project_path)
    if not config_path.exists():
        console.print(
            f"[red]No {CONFIG_FILENAME} found in {project_dir}.[/red]\n"
            f"[dim]A resumable project should contain {CONFIG_FILENAME} at its root.[/dim]"
        )
        raise SystemExit(1)

    config = HarnessConfig.from_yaml(config_path)
    if runner is None:
        _apply_saved_setup(config)
    _require_anthropic_key_for_api_mode(config.orchestration_mode)

    console.print(
        Panel(
            f"[bold]Resuming:[/bold] {config.project_name}\n"
            f"[dim]Project ID:[/dim] {config.project_id}\n"
            f"[dim]Output:[/dim] {config.output_dir}",
            title="[green]Resume[/green]",
            border_style="green",
        )
    )

    # Peek at features.json without forcing the canonical shape — it might
    # be a bare list written by an agentic runner, which is a recognized
    # state the initializer normalizes on the next phase.
    import json
    features_path = config.features_path
    if not features_path.exists():
        console.print("[dim]No features yet — starting from initialize phase.[/dim]\n")
    else:
        try:
            data = json.loads(features_path.read_text())
            if isinstance(data, dict):
                count = len(data.get("features", []))
                passing = sum(
                    1 for f in data.get("features", [])
                    if f.get("status") == "passing"
                )
                pct = round(passing / count * 100, 1) if count else 0.0
                spec_note = "with spec" if data.get("spec") else "no spec yet"
                console.print(
                    f"[dim]Progress:[/dim] {pct}% ({passing}/{count} features), "
                    f"{spec_note}, session {data.get('session_count', 0)}\n"
                )
            elif isinstance(data, list):
                console.print(
                    f"[dim]Found bare-list features.json ({len(data)} features) — "
                    f"will normalize on init phase.[/dim]\n"
                )
            else:
                console.print(
                    "[yellow]features.json shape not recognized — "
                    "init phase will recreate.[/yellow]\n"
                )
        except Exception as e:
            console.print(
                f"[yellow]Could not read features.json ({e}); "
                f"init phase will recreate.[/yellow]\n"
            )

    runner_type = _resolve_runner(config, runner)
    if runner is not None:
        config.runner_profiles = []
    _apply_model_override(config, runner_type, model)
    orchestrator = Orchestrator(config, runner_type=runner_type)
    orchestrator.run()


@main.command("import")
@click.argument("source_path", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--in-place", "in_place", is_flag=True, default=False,
    help="Write harness artifacts INTO the source repo. Default: copy into output/.",
)
@click.option(
    "--name", "name", default=None,
    help="Project name. Default: derived from source directory name.",
)
@click.option(
    "--brief", "brief", default=None,
    help="Project brief. Default: extracted from README, or prompted.",
)
@click.option(
    "--review/--no-review", "review_flag", default=None,
    help=(
        "Force review-only mode (--review) or disable auto-review for done-looking "
        "repos (--no-review). Default: auto-review when ≥80% features passing or "
        "code+tests+README without harness artifacts."
    ),
)
@click.option(
    "--review-threshold", "review_threshold",
    type=click.FloatRange(0.0, 1.0), default=0.8, show_default=True,
    help="Feature pass-rate that auto-triggers review-only mode.",
)
@click.option(
    "--runner", "-r",
    type=click.Choice([r[0] for r in _RUNNER_TABLE], case_sensitive=False),
    default=None,
    help="Runner to use (skips interactive prompt).",
)
@click.option(
    "--model", "model", default=None,
    help="Override the coding-agent model.",
)
@click.option(
    "--github-repo", "github_repo", default=None,
    help="GitHub repo for the imported project copy, e.g. owner/repo.",
)
@click.option(
    "--git-remote", "git_remote", default=None,
    help="Existing git remote URL for the imported project.",
)
@click.option(
    "--github-private/--github-public", "github_private", default=True,
    help="Visibility when --github-repo needs to create a repo.",
)
@click.option(
    "--no-git-push", "no_git_push", is_flag=True, default=False,
    help="Store GitHub/remote config but do not push automatically.",
)
def import_repo(
    source_path: str,
    in_place: bool,
    name: str | None,
    brief: str | None,
    review_flag: bool | None,
    review_threshold: float,
    runner: str | None,
    model: str | None,
    github_repo: str | None,
    git_remote: str | None,
    github_private: bool,
    no_git_push: bool,
):
    """Import an existing repo and pick up at the right phase.

    Detects what the repo already has — config / features / spec / code — and
    enters the harness pipeline at the matching point. If the repo looks
    finished (high feature pass rate, or code+tests+README without harness
    artifacts), runs ReviewerAgent and writes REVIEW.md instead of building.

    Default is to COPY the source into ./output/<slug>_<id>/. Pass --in-place
    to harness-ify the source directory directly.
    """
    import shutil
    import uuid
    from harness.import_repo import (
        EntryPhase,
        assess_repo_spec_with_agent,
        detect_stage,
    )
    from harness.runners.base import RunnerRateLimitedError

    source = Path(source_path).resolve()
    project_name = name or _slug_to_title(source.name)
    project_id = uuid.uuid4().hex[:8]
    brief_was_provided = brief is not None
    github_repo, git_remote = _normalize_project_git_inputs(github_repo, git_remote)

    # ── Step 1: Detect stage on the SOURCE first (so we can show it) ──────
    src_report = detect_stage(source, review_pass_threshold=review_threshold)
    console.print(Panel(
        f"[bold]Source:[/bold] {source}\n"
        f"[bold]Detected phase:[/bold] [cyan]{src_report.entry_phase.value}[/cyan]\n"
        + "\n".join(f"  • {r}" for r in src_report.reasons),
        title="[blue]Import — repo scan[/blue]",
        border_style="blue",
    ))

    # ── Step 2: Decide working directory ──────────────────────────────────
    if in_place:
        output_dir = source
        console.print(f"[yellow]In-place mode:[/yellow] writing harness artifacts into {output_dir}")
    else:
        slug = _slugify(project_name)
        output_dir = Path("./output") / f"{slug}_{project_id}"
        if output_dir.exists():
            raise click.UsageError(f"Refusing to overwrite existing {output_dir}")
        console.print(f"[dim]Copying source → {output_dir}[/dim]")
        shutil.copytree(source, output_dir, ignore=shutil.ignore_patterns(
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".pytest_cache", ".mypy_cache", "target",
        ))

    # ── Step 3: Resolve runner ────────────────────────────────────────────
    saved_setup = load_setup()
    runner_type = (
        RunnerType(runner)
        if runner else default_runner_from_setup(saved_setup) or _prompt_runner()
    )
    orchestration_mode = _default_orchestration_mode(runner_type)
    _require_anthropic_key_for_api_mode(orchestration_mode)

    # ── Step 4: If harness_config.json already exists, use that Harness state ─
    cfg_path = HarnessConfig.default_config_path(output_dir)
    if cfg_path.exists() and not in_place:
        config = HarnessConfig.from_yaml(cfg_path)
        config.output_dir = str(output_dir)
        config.project_id = project_id
        config.save_yaml(cfg_path)
    elif cfg_path.exists() and in_place:
        config = HarnessConfig.from_yaml(cfg_path)
        console.print(f"[dim]Existing {CONFIG_FILENAME} found — using it.[/dim]")
    else:
        # Resolve brief
        if brief is None and src_report.suggested_brief:
            console.print("\n[bold]Suggested brief from README:[/bold]")
            console.print(Panel(src_report.suggested_brief, border_style="dim"))
            if click.confirm("Use this as the brief?", default=True):
                brief = src_report.suggested_brief
        if brief is None:
            brief = click.prompt("Project brief").strip()
        if not brief:
            raise click.UsageError("Brief cannot be empty.")

        config = HarnessConfig(
            project_name=project_name,
            project_id=project_id,
            brief=brief,
            output_dir=str(output_dir),
            orchestration_mode=orchestration_mode,
            code_runner=runner_type.value,
        )
        config.save_yaml(cfg_path)

    _apply_model_override(config, runner_type, model)
    if runner is None:
        apply_setup_defaults(config, saved_setup)
    _apply_project_git_options(config, github_repo, git_remote, github_private, no_git_push)
    config.save_yaml(cfg_path)

    # ── Step 5: Re-detect on the working dir (may differ from source after copy) ─
    work_report = detect_stage(output_dir, review_pass_threshold=review_threshold)

    # ── Step 6: Decide review_only ────────────────────────────────────────
    if review_flag is True:
        review_only = True
        reason = "user passed --review"
    elif review_flag is False:
        review_only = False
        reason = "user passed --no-review"
    else:
        review_only = work_report.entry_phase == EntryPhase.REVIEW_READY
        reason = (
            "auto: repo looks done"
            if review_only else "auto: needs build/init"
        )

    confirmed_spec = None
    if not review_only and not config.features_path.exists():
        from harness.runners import create_runner

        console.print("[dim]Asking the runner to inspect the repo for existing spec material...[/dim]")
        assessment_profiles = role_profiles(config, "planner")
        assessment_runner = (
            runner_for_profile(config, assessment_profiles[0])
            if assessment_profiles else create_runner(runner_type, config)
        )
        try:
            assessment = assess_repo_spec_with_agent(
                assessment_runner,
                output_dir,
                suggested_brief=work_report.suggested_brief or config.brief,
            )
        except RunnerRateLimitedError as exc:
            Orchestrator(config, runner_type=runner_type)._handle_rate_limit(exc)
            return

        if assessment.suggested_brief and not brief_was_provided:
            config.brief = assessment.suggested_brief
            config.save_yaml(cfg_path)
        if assessment.has_spec and assessment.spec_markdown:
            confirmed_spec = assessment.spec_markdown
            config.spec_path.write_text(confirmed_spec)
            console.print(
                Panel(
                    f"[bold]Existing spec found by agent.[/bold]\n"
                    f"Confidence: {assessment.confidence:.2f}\n"
                    f"Reason: {assessment.reason or 'not provided'}\n\n"
                    f"Normalized spec written to {config.spec_path}",
                    title="[green]Import spec[/green]",
                    border_style="green",
                )
            )
        else:
            console.print(
                f"[dim]No reusable repo spec found by agent"
                f"{f' ({assessment.reason})' if assessment.reason else ''}.[/dim]"
            )

    console.print(Panel(
        f"[bold]Mode:[/bold] {'[green]review-only[/green]' if review_only else '[cyan]build[/cyan]'} "
        f"[dim]({reason})[/dim]\n"
        f"[bold]Working dir:[/bold] {output_dir}\n"
        f"[bold]Runner:[/bold] {runner_type.value}",
        title="[green]Import plan[/green]",
        border_style="green",
    ))

    orchestrator = Orchestrator(config, runner_type=runner_type)
    orchestrator.run(review_only=review_only, confirmed_spec=confirmed_spec)


def _slug_to_title(slug: str) -> str:
    return " ".join(p.capitalize() for p in slug.replace("_", "-").split("-")) or slug


@main.command("animation-theme")
@click.argument("theme_words", nargs=-1)
@click.option(
    "--runner", "-r",
    type=click.Choice(_agentic_runner_values(), case_sensitive=False),
    default=None,
    help="Signed-in coding agent to use for editing the theme.",
)
@click.option(
    "--model", "model",
    default=None,
    help="Override the coding-agent model for this theme edit.",
)
@click.option(
    "--timeout",
    default=900,
    show_default=True,
    type=click.IntRange(min=60),
    help="Maximum seconds to wait for the coding agent.",
)
def animation_theme(
    theme_words: tuple[str, ...],
    runner: str | None,
    model: str | None,
    timeout: int,
):
    """Ask Claude Code or Codex to rewrite the quiet-animation verb theme.

    The command starts the selected signed-in coding agent and gives it a
    narrow patch task: update the playful verb pools in harness/ui/spinner.py.
    The new words are then present the next time the project is run.
    """
    theme = " ".join(theme_words).strip()
    if not theme:
        theme = click.prompt("Theme / mood").strip()
    if not theme:
        raise click.UsageError("Theme cannot be empty.")

    runner_type = RunnerType(runner) if runner else _prompt_agentic_runner()
    if runner_type not in RunnerType.agentic():
        raise click.UsageError("animation-theme requires an agentic runner.")

    root = _harness_source_root()
    guide = _load_animation_theme_guide(root)
    prompt = _build_animation_theme_prompt(theme, guide)

    config = HarnessConfig(
        project_name="Agent Harness Animation Theme",
        brief=f"Customize quiet terminal animation verbs: {theme}",
        output_dir=str(root),
        orchestration_mode="runner",
        code_runner=runner_type.value,
    )
    _apply_model_override(config, runner_type, model)

    from harness.runners import create_runner
    code_runner = create_runner(runner_type, config)
    preflight = code_runner.preflight()
    if not preflight.ok:
        console.print(
            Panel(
                f"[bold]{preflight.summary}[/bold]\n\n"
                f"{preflight.error or preflight.details or 'Runner is not ready.'}",
                title="[red]Theme Agent Unavailable[/red]",
                border_style="red",
            )
        )
        raise SystemExit(1)
    if preflight.warning:
        console.print(
            Panel(
                preflight.warning,
                title="[yellow]Theme Agent Warning[/yellow]",
                border_style="yellow",
            )
        )

    console.print(
        Panel(
            f"[bold]Theme:[/bold] {theme}\n"
            f"[bold]Agent:[/bold] {runner_type.value}\n"
            f"[bold]Target:[/bold] harness/ui/spinner.py",
            title="[cyan]Animation Theme[/cyan]",
            border_style="cyan",
        )
    )
    result = code_runner.implement(prompt, cwd=str(root), timeout_seconds=timeout)
    if not result.success:
        console.print(
            Panel(
                result.error or "The theme agent exited without a usable result.",
                title="[red]Animation Theme Failed[/red]",
                border_style="red",
            )
        )
        raise SystemExit(1)

    console.print("[green]Animation theme agent finished.[/green]")
    if result.output:
        console.print(
            Panel(result.output[-3000:], title="Agent Summary", border_style="green")
        )


@main.command()
@click.argument("config_file", type=click.Path(exists=True))
def status(config_file: str):
    """Print current progress for a project."""
    config = HarnessConfig.from_yaml(config_file)
    tracker = ProgressTracker(config)
    try:
        progress = tracker.load()
    except FileNotFoundError:
        console.print("[red]No features file found. Run 'harness run' first.[/red]")
        raise SystemExit(1)

    console.print(f"\n[bold]{progress.project_name}[/bold]")
    console.print(f"Brief: {progress.brief}")
    console.print(
        f"Progress: [green]{progress.completion_pct}%[/green] "
        f"({len(progress.passing_features)}/{len(progress.features)} features)"
    )
    console.print(f"Sessions: {progress.session_count}")
    console.print()

    for feature in sorted(progress.features, key=lambda f: f.priority):
        icon = {
            "pending": "⬜",
            "in_progress": "🔄",
            "passing": "✅",
            "failing": "❌",
        }.get(feature.status.value, "?")
        score = ""
        if feature.latest_evaluation:
            score = f" ({feature.latest_evaluation.overall_score:.1f}/10)"
        console.print(f"  {icon} [{feature.id}] {feature.name}{score}")


@main.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.argument("brief")
@click.option("--project-name", default=None, help="Override project name from config")
def init(config_file: str, brief: str, project_name: str | None):
    """Initialize a new project (features.json, init.sh, first git commit)."""
    config = HarnessConfig.from_yaml(config_file)
    _apply_saved_setup(config)
    if project_name:
        config.project_name = project_name
    config.brief = brief

    from harness.agents import InitializerAgent
    from harness.project_git import sync_project_git

    init_runner = None
    if config.orchestration_mode == "runner":
        init_profiles = role_profiles(config, "initializer")
        if init_profiles:
            init_runner = runner_for_profile(config, init_profiles[0])
        else:
            from harness.runners import create_runner
            init_runner = create_runner(RunnerType(config.code_runner or "subprocess"), config)

    agent = InitializerAgent(config, runner=init_runner)
    progress = agent.run(brief=brief)
    _print_project_git_sync_result(sync_project_git(config, reason="manual init"))
    console.print(
        f"[green]Initialized {len(progress.features)} features "
        f"for '{config.project_name}'[/green]"
    )


@main.command()
@click.argument("config_file", type=click.Path(exists=True))
def plan(config_file: str):
    """Run only the planner agent to expand the brief into a spec."""
    config = HarnessConfig.from_yaml(config_file)
    _apply_saved_setup(config)
    tracker = ProgressTracker(config)
    progress = tracker.load()

    from harness.agents import PlannerAgent
    planner_runner = None
    if config.orchestration_mode == "runner":
        planner_profiles = role_profiles(config, "planner")
        if planner_profiles:
            planner_runner = runner_for_profile(config, planner_profiles[0])
        else:
            from harness.runners import create_runner
            planner_runner = create_runner(RunnerType(config.code_runner or "subprocess"), config)
    agent = PlannerAgent(config, runner=planner_runner)
    spec = agent.run(brief=config.brief)
    progress.spec = spec
    tracker.save(progress)
    console.print("[green]Spec written to features file.[/green]")


@main.command()
def runners():
    """List all available runners with their billing and requirements."""
    _print_runner_menu()


if __name__ == "__main__":
    main()
