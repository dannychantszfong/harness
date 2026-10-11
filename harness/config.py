from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional
import json
import platform
import re
import uuid
import yaml
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_FILENAME = "harness_config.json"

# Import here to avoid circular; config only stores the string value.
# Runners are resolved at runtime by create_runner().
_RUNNER_CHOICES = ["subprocess", "sdk", "codex"]

# harness_config.json lives in the output project's repo, which the
# initializer and generator commit and project_git pushes. Secret-looking
# values must therefore be env references, never literals (US-21/US-23).
_ENV_REFERENCE_RE = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})$")
_SECRET_KEY_RE = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)


def is_env_reference(value: str) -> bool:
    return bool(_ENV_REFERENCE_RE.match(value.strip()))


def looks_like_secret_key(name: str) -> bool:
    return bool(_SECRET_KEY_RE.search(name))


def _literal_secret_error(where: str, example: str) -> ValueError:
    return ValueError(
        f"{where} holds a literal secret. Use an environment reference such as "
        f'"${example}" instead — {CONFIG_FILENAME} is committed to the project repo.'
    )


def check_env_secrets(env: dict[str, str], where: str) -> None:
    for key, value in (env or {}).items():
        if looks_like_secret_key(key) and value and not is_env_reference(str(value)):
            raise _literal_secret_error(f"{where}[{key!r}]", key.upper())


_SECRET_FLAG_RE = re.compile(r"^--?[\w-]*(?:key|token|secret|password)[\w-]*$", re.IGNORECASE)


def check_arg_secrets(args: list[str], where: str) -> None:
    """Reject `--api-key sk-…` / `--token=…` style literals in CLI args.

    Extra args are passed to the agent CLI verbatim (no $VAR expansion), so
    the fix is to move the secret into an env var.
    """
    for i, arg in enumerate(args or []):
        flag, eq, inline = str(arg).partition("=")
        if not _SECRET_FLAG_RE.match(flag):
            continue
        value = inline if eq else (str(args[i + 1]) if i + 1 < len(args) else "")
        if value and not value.startswith("-"):
            raise ValueError(
                f"{where} passes a secret on the command line ({flag} …). Put it in an "
                f"environment variable instead — {CONFIG_FILENAME} is committed to the project repo."
            )


def check_url_credentials(url: Optional[str], where: str) -> None:
    """Reject http(s) remotes with embedded credentials (https://token@host/...)."""
    if not url:
        return
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https") and (parsed.username or parsed.password):
        raise ValueError(
            f"{where} embeds credentials in the URL. Use a plain URL and let "
            "`gh auth` or a git credential helper supply them."
        )


class EvaluatorWeights(BaseModel):
    design_quality: float = 0.30
    originality: float = 0.30
    craft: float = 0.25
    functionality: float = 0.15

    def validate_sum(self) -> None:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Evaluator weights must sum to 1.0, got {total}")


class RunnerProfile(BaseModel):
    """A named coding-agent runtime users can place in role fallback chains."""

    name: str
    runner: Literal["subprocess", "sdk", "codex"]
    model: Optional[str] = None
    provider: Literal[
        "subscription",
        "anthropic_api",
        "openai_api",
        "openrouter",
        "gemini",
        "custom",
    ] = "subscription"
    env: dict[str, str] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    codex_oss: bool = False
    codex_local_provider: Optional[Literal["lmstudio", "ollama"]] = None
    enabled: bool = True

    @field_validator("env")
    @classmethod
    def _env_secrets_are_references(cls, env: dict[str, str]) -> dict[str, str]:
        check_env_secrets(env, "runner profile env")
        return env

    @field_validator("extra_args")
    @classmethod
    def _no_secret_args(cls, args: list[str]) -> list[str]:
        check_arg_secrets(args, "runner profile extra_args")
        return args


class HarnessConfig(BaseModel):
    project_name: str
    brief: str
    output_dir: str = "./output"

    # Model selection — defaults to latest Opus
    planner_model: str = "claude-opus-4-7"
    generator_model: str = "claude-opus-4-7"
    evaluator_model: str = "claude-opus-4-7"

    # Context management
    # Resets outperform compaction for Opus 4.5+; keep threshold conservative.
    context_reset_threshold_tokens: int = 150_000

    # Runner session limits. A single feature implementation routinely takes
    # longer than 10 minutes, so the default is 30. planner/evaluator
    # sessions may override it (None → runner_timeout_seconds).
    runner_timeout_seconds: int = Field(default=1800, ge=60)
    planner_timeout_seconds: Optional[int] = Field(default=None, ge=60)
    evaluator_timeout_seconds: Optional[int] = Field(default=None, ge=60)
    # max_turns passed to the Claude Code SDK (sdk runner only).
    sdk_max_turns: int = Field(default=100, ge=1)

    # Generator / evaluator loop
    max_iterations_per_feature: int = 15
    evaluator_pass_score: float = 8.0  # out of 10
    evaluator_weights: EvaluatorWeights = Field(default_factory=EvaluatorWeights)
    # A feature passes only if the weighted score clears evaluator_pass_score
    # AND functionality is at least this (ADR-013): 9/9/9/3 must not pass.
    evaluator_functionality_floor: float = Field(default=6.0, ge=0, le=10)

    # Role-appropriate permissions in runner mode (C-06). Roles listed in
    # read_only_roles run without write tools (Claude Code: dontAsk + an
    # allow-list; Codex: --sandbox read-only). role_allowed_tools overrides
    # the Claude Code tool list for any role, in Claude Code permission-rule
    # syntax, e.g. {"evaluator": ["Read", "Grep", "Bash(npm test *)"]}.
    # Roles: planner, initializer, generator, evaluator, reviewer, contract.
    read_only_roles: list[str] = Field(
        default_factory=lambda: ["planner", "evaluator", "contract"]
    )
    role_allowed_tools: dict[str, list[str]] = Field(default_factory=dict)
    # The project's test command, allow-listed for read-only roles and the
    # api-mode evaluator's run_command tool (e.g. "npm test", "pytest -q").
    test_command: Optional[str] = None

    # Experimental (C-07): give the runner-mode evaluator a Playwright MCP
    # browser to exercise acceptance criteria (Claude Code runners only —
    # ignored with a warning on Codex). evaluator_mcp_config points at an MCP
    # config JSON ({"mcpServers": {...}}); None → Harness writes
    # .harness/playwright-mcp.json running `npx -y @playwright/mcp@latest`.
    evaluator_browser_verification: bool = False
    evaluator_mcp_config: Optional[str] = None

    # api mode only (ADR-016): the evaluator inspects the project through a
    # bounded tool loop before it must call submit_evaluation.
    api_tool_rounds: int = Field(default=20, ge=0)
    api_command_timeout_seconds: int = Field(default=120, ge=5)

    # Progress files (written inside output_dir)
    features_file: str = "features.json"
    progress_file: str = "progress.md"
    spec_file: str = "spec.md"

    # Init script — the per-project bootstrap that the generator and
    # evaluator both run to start the app for testing.
    #
    # Two layers, both Optional, both with platform-aware defaults:
    #   • init_script_type — "bash" | "powershell" | "cmd". Determines the
    #     filename suffix, the file's header line, the chmod policy, and
    #     the prompt the initializer agent sees. None → derived: bash on
    #     macOS/Linux, powershell on Windows.
    #   • init_script — the filename inside output_dir. None → derived
    #     from init_script_type: init.sh / init.ps1 / init.bat.
    #
    # Use `effective_init_script_type` and `effective_init_script` to
    # read the resolved values; never read these fields directly.
    init_script: Optional[str] = None
    init_script_type: Optional[Literal["bash", "powershell", "cmd"]] = None
    startup_command: Optional[str] = None

    # Sprint contracts
    sprint_contract_enabled: bool = True

    # When the runner reports a subscription usage cap, schedule an OS-native
    # job to re-run `harness resume` shortly after the reset time.
    auto_resume_on_rate_limit: bool = True

    # Per-output-project GitHub sync. The Harness repo ignores output/; each
    # generated project is its own git repo and can push to its own remote.
    project_git_push: bool = False
    project_git_branch: str = "main"
    project_git_remote: Optional[str] = None      # e.g. git@github.com:owner/repo.git
    project_github_repo: Optional[str] = None     # e.g. owner/repo
    project_github_private: bool = True

    # Terminal feel for quiet waits. These only render in interactive terminals.
    progress_animation: Literal[
        "sparkle",
        "bloom",
        "snow",
        "braille",
        "orbit",
        "pulse",
        "dots",
        "moon",
        "bars",
        "clock",
        "wave",
        "tech",
    ] = "sparkle"
    progress_phrase_style: Literal["steady", "playful"] = "playful"
    progress_text_effect: Literal["none", "typewriter", "scramble"] = "typewriter"

    # Unique project identifier — auto-generated when using `harness new`
    project_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])

    # ── Orchestration mode ──────────────────────────────────────────────────
    # "runner" — planner + evaluator use the same runner as the generator.
    #            Subscription runners need no ANTHROPIC_API_KEY. (default)
    # "api"    — planner + evaluator always call the Anthropic API directly.
    #            ANTHROPIC_API_KEY is always required.
    orchestration_mode: Literal["api", "runner"] = "runner"

    # ── Runner selection ────────────────────────────────────────────────────
    # Which coding-agent the GeneratorAgent uses to implement features.
    # Only the three agentic runners exist as first-class options. Direct
    # API providers (Anthropic, OpenAI, Gemini, OpenRouter) plug into one
    # of these via env vars — see the api_*_key fields below.
    # Leave as None to be prompted interactively at startup.
    # Options: subprocess | sdk | codex
    code_runner: Optional[str] = None

    # Model/provider knobs for agentic coding runtimes.
    # code_runner_model is passed to Claude Code/Codex as their session model.
    # For Codex, codex_oss/codex_local_provider expose local-provider routing.
    code_runner_model: Optional[str] = None
    codex_oss: bool = False
    codex_local_provider: Optional[Literal["lmstudio", "ollama"]] = None
    code_runner_extra_args: list[str] = Field(default_factory=list)

    # Resolved env vars projected onto every coding-agent invocation.
    #
    # Two layers feed this single dict:
    #
    #   1. The user-authored *base* env stored in this field — defaults
    #      that apply regardless of which profile the orchestrator picked.
    #      Set this in config when a value is the same across every
    #      profile (e.g. PATH adjustments, project-wide flags).
    #
    #   2. `RunnerProfile.env` — per-profile env (e.g. ANTHROPIC_BASE_URL
    #      for an OpenRouter profile). When `runner_profiles.config_for_profile`
    #      activates a profile, the active profile's env MERGES OVER the
    #      base: per-key, profile values win on collision, base keys
    #      survive when the profile doesn't override them.
    #
    # Naming history: this field was previously `code_runner_env`. The
    # `validation_alias` keeps existing configs loading under the old key.
    # Always read via `self.active_runner_env` at runtime; the aliases are
    # for serialization compat only.
    #
    # Values like "$OPENROUTER_API_KEY" or "${VAR}" are resolved against
    # the parent process env at call time
    # (see runners/base._expand_env_value).
    active_runner_env: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("active_runner_env", "code_runner_env"),
        serialization_alias="active_runner_env",
    )

    # Optional role-aware runner rotation. Each order is a whitelist: the
    # orchestrator tries profiles in order and moves to the next profile when
    # the current runner reports a usage cap.
    runner_profiles: list[RunnerProfile] = Field(default_factory=list)
    planner_runner_order: list[str] = Field(default_factory=list)
    generator_runner_order: list[str] = Field(default_factory=list)
    evaluator_runner_order: list[str] = Field(default_factory=list)
    reviewer_runner_order: list[str] = Field(default_factory=list)
    fallback_on_rate_limit: bool = True

    # API-provider keys. These do NOT spawn standalone runners. They are
    # consumed by the three agentic runners as their underlying models:
    #   • ANTHROPIC_API_KEY  →  Claude Code / SDK (default Anthropic auth)
    #   • OPENAI_API_KEY     →  Codex
    #   • GEMINI_API_KEY     →  reserved for future Codex multi-provider use
    #   • OPENROUTER_API_KEY →  set ANTHROPIC_BASE_URL=https://openrouter.ai/...
    #                          and ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY
    #                          to route Claude Code through OpenRouter.
    # The harness does NOT auto-export these to the subprocess env — set them
    # in your shell or use a tool like direnv. The fields exist so you can
    # persist what a project expects in harness_config.json as documentation,
    # which is why they must be env references ("$OPENAI_API_KEY").
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None

    @field_validator("openai_api_key", "gemini_api_key", "openrouter_api_key")
    @classmethod
    def _api_keys_are_references(cls, value: Optional[str], info) -> Optional[str]:
        if value and not is_env_reference(value):
            raise _literal_secret_error(info.field_name, info.field_name.upper())
        return value

    @model_validator(mode="after")
    def _runner_env_secrets_are_references(self) -> "HarnessConfig":
        check_env_secrets(self.active_runner_env, "active_runner_env")
        check_arg_secrets(self.code_runner_extra_args, "code_runner_extra_args")
        check_url_credentials(self.project_git_remote, "project_git_remote")
        return self

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "HarnessConfig":
        # P-06: the overall score is computed from these, so a typo must
        # fail on load rather than silently skew every grade.
        self.evaluator_weights.validate_sum()
        return self

    def check_secrets(self) -> None:
        """Re-run the literal-secret checks (fields may be assigned after load)."""
        for name in ("openai_api_key", "gemini_api_key", "openrouter_api_key"):
            value = getattr(self, name)
            if value and not is_env_reference(value):
                raise _literal_secret_error(name, name.upper())
        check_env_secrets(self.active_runner_env, "active_runner_env")
        check_arg_secrets(self.code_runner_extra_args, "code_runner_extra_args")
        check_url_credentials(self.project_git_remote, "project_git_remote")
        for profile in self.runner_profiles:
            check_env_secrets(profile.env, f"runner profile {profile.name!r} env")
            check_arg_secrets(profile.extra_args, f"runner profile {profile.name!r} extra_args")

    @classmethod
    def from_file(cls, path: str | Path) -> "HarnessConfig":
        path = Path(path)
        text = path.read_text()
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
        return cls(**data)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        """Backward-compatible name; reads JSON when the file suffix is .json."""
        return cls.from_file(path)

    @classmethod
    def load_project(cls, project_dir: str | Path) -> "HarnessConfig":
        """Load <project_dir>/harness_config.json with output_dir pinned to it.

        A project's config lives at its root, so the directory it was found
        in *is* the output dir — whatever (possibly relative, P-01) path the
        file recorded, and whatever the current working directory is.
        """
        project_dir = Path(project_dir).resolve()
        config = cls.from_file(cls.default_config_path(project_dir))
        config.output_dir = str(project_dir)
        return config

    def save_file(self, path: str | Path) -> None:
        """Serialize config to JSON by default, YAML only for .yaml/.yml paths.

        output_dir is persisted as an absolute path (P-01): `harness resume`
        and the auto-resume scheduler run from other working directories.
        """
        path = Path(path)
        self.check_secrets()
        self.output_dir = str(Path(self.output_dir).resolve())
        data = self.model_dump(mode="json")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".json":
            path.write_text(json.dumps(data, indent=2) + "\n")
        else:
            with open(path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def save_yaml(self, path: str | Path) -> None:
        """Backward-compatible name; writes JSON when the file suffix is .json."""
        self.save_file(path)

    @staticmethod
    def default_config_path(project_dir: str | Path) -> Path:
        return Path(project_dir) / CONFIG_FILENAME

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir)

    @property
    def features_path(self) -> Path:
        return self.output_path / self.features_file

    @property
    def progress_path(self) -> Path:
        return self.output_path / self.progress_file

    @property
    def effective_init_script_type(self) -> str:
        """Resolved init script type: bash | powershell | cmd.

        Priority: explicit init_script_type → suffix of explicit init_script
        → host platform default (powershell on Windows, bash elsewhere).
        """
        if self.init_script_type:
            return self.init_script_type
        if self.init_script:
            suffix = Path(self.init_script).suffix.lower()
            if suffix == ".ps1":
                return "powershell"
            if suffix in {".bat", ".cmd"}:
                return "cmd"
            if suffix == ".sh":
                return "bash"
        return "powershell" if platform.system() == "Windows" else "bash"

    @property
    def effective_init_script(self) -> str:
        """Resolved init script filename. Derived from type if not set."""
        if self.init_script:
            return self.init_script
        return {
            "bash": "init.sh",
            "powershell": "init.ps1",
            "cmd": "init.bat",
        }[self.effective_init_script_type]

    @property
    def init_script_path(self) -> Path:
        return self.output_path / self.effective_init_script

    @property
    def startup_command_for_platform(self) -> str:
        if self.startup_command:
            return self.startup_command
        script_type = self.effective_init_script_type
        script = self.effective_init_script
        if script_type == "powershell":
            return f"powershell -ExecutionPolicy Bypass -File {script}"
        if script_type == "cmd":
            return script
        return f"bash {script}"

    @property
    def spec_path(self) -> Path:
        return self.output_path / self.spec_file

    @property
    def billing_mode(self) -> Literal["subscription", "api"]:
        """How usage is paid for, for cost display (C-04).

        "subscription" when every agent runs through coding-agent runners on
        subscription profiles (the default); "api" as soon as the Anthropic
        API orchestrates or a profile bills an API provider. On a
        subscription the runtimes' cost figure is an API-price equivalent.
        """
        if self.orchestration_mode == "api":
            return "api"
        enabled = [p for p in self.runner_profiles if p.enabled]
        if enabled and any(p.provider != "subscription" for p in enabled):
            return "api"
        return "subscription"
