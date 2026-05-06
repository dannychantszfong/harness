"""Runner profile setup and role-aware fallback helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from harness.config import HarnessConfig, RunnerProfile
from harness.runners import RunnerType, create_runner
from harness.runners.base import CodeRunner


ROLE_ORDER_FIELDS = {
    "initializer": "planner_runner_order",
    "planner": "planner_runner_order",
    "generator": "generator_runner_order",
    "evaluator": "evaluator_runner_order",
    "reviewer": "reviewer_runner_order",
}


class HarnessSetup(BaseModel):
    runner_profiles: list[RunnerProfile] = Field(default_factory=list)
    planner_runner_order: list[str] = Field(default_factory=list)
    generator_runner_order: list[str] = Field(default_factory=list)
    evaluator_runner_order: list[str] = Field(default_factory=list)
    reviewer_runner_order: list[str] = Field(default_factory=list)
    fallback_on_rate_limit: bool = True


def default_setup_path() -> Path:
    override = os.environ.get("HARNESS_SETUP_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".harness" / "setup.json"


def load_setup(path: str | Path | None = None) -> HarnessSetup | None:
    setup_path = Path(path).expanduser() if path else default_setup_path()
    if not setup_path.exists():
        return None
    return HarnessSetup(**json.loads(setup_path.read_text()))


def save_setup(setup: HarnessSetup, path: str | Path | None = None) -> Path:
    setup_path = Path(path).expanduser() if path else default_setup_path()
    setup_path.parent.mkdir(parents=True, exist_ok=True)
    setup_path.write_text(json.dumps(setup.model_dump(mode="json"), indent=2) + "\n")
    return setup_path


def apply_setup_defaults(config: HarnessConfig, setup: HarnessSetup | None = None) -> bool:
    setup = setup if setup is not None else load_setup()
    if setup is None or not setup.runner_profiles:
        return False

    changed = False
    if not config.runner_profiles:
        config.runner_profiles = [p.model_copy(deep=True) for p in setup.runner_profiles]
        changed = True

    for field in (
        "planner_runner_order",
        "generator_runner_order",
        "evaluator_runner_order",
        "reviewer_runner_order",
    ):
        if not getattr(config, field) and getattr(setup, field):
            setattr(config, field, list(getattr(setup, field)))
            changed = True

    if config.fallback_on_rate_limit != setup.fallback_on_rate_limit:
        config.fallback_on_rate_limit = setup.fallback_on_rate_limit
        changed = True

    first_generator = first_profile_for_role(config, "generator")
    if first_generator is not None:
        if not config.code_runner:
            config.code_runner = first_generator.runner
            changed = True
        if not config.code_runner_model and first_generator.model:
            config.code_runner_model = first_generator.model
            changed = True
        if config.orchestration_mode != "runner":
            config.orchestration_mode = "runner"
            changed = True

    return changed


def default_runner_from_setup(setup: HarnessSetup | None = None) -> RunnerType | None:
    setup = setup if setup is not None else load_setup()
    if setup is None or not setup.runner_profiles:
        return None
    order = setup.generator_runner_order or [p.name for p in setup.runner_profiles if p.enabled]
    profiles = {p.name: p for p in setup.runner_profiles if p.enabled}
    for name in order:
        profile = profiles.get(name)
        if profile:
            return RunnerType(profile.runner)
    return None


def first_profile_for_role(config: HarnessConfig, role: str) -> RunnerProfile | None:
    profiles = role_profiles(config, role)
    return profiles[0] if profiles else None


def role_profiles(config: HarnessConfig, role: str) -> list[RunnerProfile]:
    enabled = {p.name: p for p in config.runner_profiles if p.enabled}
    if not enabled:
        return []

    field = ROLE_ORDER_FIELDS.get(role, "generator_runner_order")
    names = list(getattr(config, field) or [])

    # A reviewer can reasonably reuse evaluator policy unless explicitly set.
    if role == "reviewer" and not names:
        names = list(config.evaluator_runner_order or [])

    if not names:
        names = [p.name for p in config.runner_profiles if p.enabled]

    ordered = [enabled[name] for name in names if name in enabled]
    return ordered or list(enabled.values())


def runner_for_profile(config: HarnessConfig, profile: RunnerProfile) -> CodeRunner:
    return create_runner(RunnerType(profile.runner), config_for_profile(config, profile))


def config_for_profile(config: HarnessConfig, profile: RunnerProfile) -> HarnessConfig:
    data = config.model_dump(mode="python")
    data["code_runner"] = profile.runner
    data["code_runner_model"] = profile.model if profile.model is not None else config.code_runner_model
    data["code_runner_extra_args"] = list(profile.extra_args)
    data["code_runner_env"] = dict(profile.env)
    data["codex_oss"] = profile.codex_oss
    data["codex_local_provider"] = profile.codex_local_provider
    return HarnessConfig(**data)
