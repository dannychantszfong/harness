"""CLI entry point for the Agent Harness."""

import re
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from pathlib import Path

from harness.config import HarnessConfig
from harness.orchestrator import Orchestrator
from harness.progress.tracker import ProgressTracker
from harness.runners.base import RunnerType

console = Console()


# ── Runner selection ──────────────────────────────────────────────────────────

_RUNNER_TABLE = [
    # (value, family, billing, file_i/o, requires)
    ("subprocess", "Agentic", "Claude subscription", "✅ full", "`claude` CLI installed"),
    ("sdk",        "Agentic", "Claude subscription", "✅ full", "pip install claude-code-sdk"),
    ("codex",      "Agentic", "OpenAI subscription", "✅ full", "`codex` CLI installed"),
    ("anthropic",  "API",     "Pay-per-token",        "❌ text only", "ANTHROPIC_API_KEY"),
    ("openai",     "API",     "Pay-per-token",        "❌ text only", "OPENAI_API_KEY + pip install openai"),
    ("gemini",     "API",     "Pay-per-token",        "❌ text only", "GEMINI_API_KEY + pip install google-generativeai"),
    ("openrouter", "API",     "Pay-per-token",        "❌ text only", "OPENROUTER_API_KEY + pip install openai"),
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
_FLAG_TO_RUNNER: dict[str, RunnerType] = {
    "claude_code":   RunnerType.SUBPROCESS,
    "claude_sdk":    RunnerType.SDK,
    "codex":         RunnerType.CODEX,
    "anthropic_api": RunnerType.ANTHROPIC,
    "openai_api":    RunnerType.OPENAI,
    "gemini":        RunnerType.GEMINI,
    "openrouter":    RunnerType.OPENROUTER,
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
    """Subscription runners default to 'runner' mode (no API key needed).
    API runners are always locked to 'api' mode.
    """
    return "runner" if runner_type in RunnerType.agentic() else "api"


def _model_prompt_hint(runner_type: RunnerType) -> str:
    """Short hint for the coding-agent model prompt."""
    if runner_type in (RunnerType.SUBPROCESS, RunnerType.SDK):
        return "Claude Code model alias or full ID, e.g. sonnet, opus, claude-sonnet-4-6"
    if runner_type == RunnerType.CODEX:
        return "Codex model ID, e.g. gpt-5.2, gpt-5.4, or a local OSS model"
    if runner_type == RunnerType.OPENAI:
        return "OpenAI model ID, e.g. gpt-5.4"
    if runner_type == RunnerType.GEMINI:
        return "Gemini model ID, e.g. gemini-2.5-pro"
    if runner_type == RunnerType.OPENROUTER:
        return "OpenRouter model ID, e.g. anthropic/claude-sonnet-4-6"
    return "Anthropic model ID, e.g. claude-sonnet-4-6"


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
        ("--anthropic-api", "anthropic_api", "Anthropic API     (pay-per-token, ANTHROPIC_API_KEY)"),
        ("--openai-api",    "openai_api",    "OpenAI API        (pay-per-token, OPENAI_API_KEY)"),
        ("--gemini",        "gemini",        "Google Gemini API (pay-per-token, GEMINI_API_KEY)"),
        ("--openrouter",    "openrouter",    "OpenRouter        (pay-per-token, OPENROUTER_API_KEY)"),
    ]
    for flag, param, help_text in reversed(flags):
        f = click.option(flag, param, is_flag=True, default=False, help=help_text)(f)
    return f


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
        "Claude/Codex runners pass this to the CLI; API runners use it as generator_model."
    ),
)
@_runner_flags
def new(runner: str | None, with_api: bool, model: str | None,
        claude_code: bool, claude_sdk: bool,
        codex: bool, anthropic_api: bool, openai_api: bool, gemini: bool, openrouter: bool):
    """Create a new project interactively — no YAML needed.

    Pick your runner via a named flag, or leave all flags off to get an
    interactive menu.

    \b
    Subscription runners (no extra cost beyond your plan):
      harness new --claude-code     Claude Code CLI
      harness new --claude-sdk      Claude Code SDK
      harness new --codex           OpenAI Codex CLI

    \b
    API runners (pay-per-token):
      harness new --anthropic-api   Anthropic API
      harness new --openai-api      OpenAI API
      harness new --gemini          Google Gemini API
      harness new --openrouter      OpenRouter (any model)
    """
    console.print(
        Panel(
            "[bold]Agent Harness[/bold]\nNew Project Setup",
            subtitle="Let's build something",
            border_style="blue",
        )
    )
    console.print()

    # ── Step 1: Resolve runner FIRST so the planner uses the right mode ───
    # Named flags > --runner > interactive prompt. (No config file at this stage.)
    named = _pick_runner_from_flags(
        claude_code=claude_code, claude_sdk=claude_sdk, codex=codex,
        anthropic_api=anthropic_api, openai_api=openai_api,
        gemini=gemini, openrouter=openrouter,
    )
    if named:
        runner_type = named
    elif runner:
        runner_type = RunnerType(runner)
    else:
        runner_type = _prompt_runner()

    # ── Step 2: Derive orchestration mode and validate keys up front ──────
    # API runners are locked to "api" mode.
    # Subscription runners default to "runner" mode unless --with-api is passed.
    if runner_type in RunnerType.api_based():
        orchestration_mode = "api"
    else:
        orchestration_mode = "api" if with_api else "runner"
    _require_anthropic_key_for_api_mode(orchestration_mode)

    code_model = _resolve_model_choice(runner_type, model)

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
    _apply_model_override(config, runner_type, code_model)

    # Create the output dir now — the runner-mode planner uses it as cwd.
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 6: In runner mode the planner needs a runner instance ────────
    from harness.runners import create_runner
    planner_runner = create_runner(runner_type, config) if orchestration_mode == "runner" else None

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
    config_path = output_dir / "config.yaml"
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
    """Run the full harness for a project defined in CONFIG_FILE (YAML)."""
    config = HarnessConfig.from_yaml(config_file)
    _require_anthropic_key_for_api_mode(config.orchestration_mode)

    console.print(
        Panel(
            f"[bold]Agent Harness[/bold]\n{config.project_name}",
            subtitle=config.brief[:80],
        )
    )

    runner_type = _resolve_runner(config, runner)
    _apply_model_override(config, runner_type, model)

    orchestrator = Orchestrator(config, runner_type=runner_type)
    orchestrator.run()


@main.command()
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--runner", "-r",
    type=click.Choice([r[0] for r in _RUNNER_TABLE], case_sensitive=False),
    default=None,
    help="Override the runner saved in config.yaml (rarely needed).",
)
@click.option(
    "--model", "model", default=None,
    help="Override the coding-agent model for this resume only.",
)
def resume(project_dir: str, runner: str | None, model: str | None):
    """Resume work on an existing project.

    Pass the project's output directory (the one containing config.yaml).
    The harness picks up wherever it left off:

      • initialize step skips if features.json already exists
      • plan step skips if spec.md already exists
      • feature loop continues from the next pending feature
    """
    project_path = Path(project_dir)
    config_path = project_path / "config.yaml"
    if not config_path.exists():
        console.print(
            f"[red]No config.yaml found in {project_dir}.[/red]\n"
            f"[dim]A resumable project should contain config.yaml at its root.[/dim]"
        )
        raise SystemExit(1)

    config = HarnessConfig.from_yaml(config_path)
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
    _apply_model_override(config, runner_type, model)
    orchestrator = Orchestrator(config, runner_type=runner_type)
    orchestrator.run()


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
    if project_name:
        config.project_name = project_name
    config.brief = brief

    from harness.agents import InitializerAgent
    agent = InitializerAgent(config)
    progress = agent.run(brief=brief)
    console.print(
        f"[green]Initialized {len(progress.features)} features "
        f"for '{config.project_name}'[/green]"
    )


@main.command()
@click.argument("config_file", type=click.Path(exists=True))
def plan(config_file: str):
    """Run only the planner agent to expand the brief into a spec."""
    config = HarnessConfig.from_yaml(config_file)
    tracker = ProgressTracker(config)
    progress = tracker.load()

    from harness.agents import PlannerAgent
    agent = PlannerAgent(config)
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
