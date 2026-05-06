from pydantic import BaseModel, Field
from typing import Literal, Optional
import json
import uuid
import yaml
from pathlib import Path

CONFIG_FILENAME = "harness_config.json"

# Import here to avoid circular; config only stores the string value.
# Runners are resolved at runtime by create_runner().
_RUNNER_CHOICES = ["subprocess", "sdk", "codex"]


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

    # Generator / evaluator loop
    max_iterations_per_feature: int = 15
    evaluator_pass_score: float = 8.0  # out of 10
    evaluator_weights: EvaluatorWeights = Field(default_factory=EvaluatorWeights)

    # Progress files (written inside output_dir)
    features_file: str = "features.json"
    progress_file: str = "progress.md"
    init_script: str = "init.sh"
    spec_file: str = "spec.md"

    # Sprint contracts
    sprint_contract_enabled: bool = True

    # When the runner reports a subscription usage cap, schedule a launchd
    # job to re-run `harness resume` shortly after the reset time. macOS only.
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
    #            Subscription runners need no ANTHROPIC_API_KEY.
    # "api"    — planner + evaluator always call the Anthropic API directly.
    #            ANTHROPIC_API_KEY is always required.
    orchestration_mode: Literal["api", "runner"] = "api"

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
    code_runner_env: dict[str, str] = Field(default_factory=dict)

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
    # persist what a project expects in harness_config.json as documentation.
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None

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

    def save_file(self, path: str | Path) -> None:
        """Serialize config to JSON by default, YAML only for .yaml/.yml paths."""
        path = Path(path)
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
    def init_script_path(self) -> Path:
        return self.output_path / self.init_script

    @property
    def spec_path(self) -> Path:
        return self.output_path / self.spec_file
