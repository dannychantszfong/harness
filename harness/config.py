from pydantic import BaseModel, Field
from typing import Literal, Optional
import uuid
import yaml
from pathlib import Path

# Import here to avoid circular; config only stores the string value.
# Runners are resolved at runtime by create_runner().
_RUNNER_CHOICES = [
    "subprocess", "sdk", "codex",           # agentic
    "anthropic", "openai", "gemini", "openrouter",  # api
]


class EvaluatorWeights(BaseModel):
    design_quality: float = 0.30
    originality: float = 0.30
    craft: float = 0.25
    functionality: float = 0.15

    def validate_sum(self) -> None:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Evaluator weights must sum to 1.0, got {total}")


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
    # Which engine the GeneratorAgent uses to implement features.
    # Leave as None to be prompted interactively at startup.
    # Options: subprocess | sdk | codex | anthropic | openai | gemini | openrouter
    code_runner: Optional[str] = None

    # Model/provider knobs for agentic coding runtimes.
    # code_runner_model is passed to Claude Code/Codex as their session model.
    # For Codex, codex_oss/codex_local_provider expose local-provider routing.
    code_runner_model: Optional[str] = None
    codex_oss: bool = False
    codex_local_provider: Optional[Literal["lmstudio", "ollama"]] = None
    code_runner_extra_args: list[str] = Field(default_factory=list)

    # API keys for non-Anthropic providers (can also be set via env vars)
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def save_yaml(self, path: str | Path) -> None:
        """Serialize config back to a YAML file."""
        data = self.model_dump()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

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
