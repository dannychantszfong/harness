"""Tests for the env-layer contract: base vs profile env, merge semantics, alias.

The harness has two layers of env config that get projected into a single
runtime dict (`HarnessConfig.active_runner_env`):

  • Base — the user-authored `active_runner_env` on HarnessConfig.
  • Profile — a `RunnerProfile.env`, applied when the orchestrator picks
    that profile.

When a profile is active, profile keys MERGE OVER the base. Base keys
that aren't overridden by the profile must still be present.

The field used to be called `code_runner_env`. A pydantic alias keeps
old configs loading; new configs serialize under the new name.
"""

from pathlib import Path

import pytest

from harness.config import HarnessConfig, RunnerProfile
from harness.runner_profiles import config_for_profile


def _config(tmp_path: Path, **kwargs) -> HarnessConfig:
    return HarnessConfig(
        project_name="t",
        brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
        code_runner="subprocess",
        **kwargs,
    )


# ── Field rename + backward-compat alias ─────────────────────────────────────

def test_active_runner_env_is_canonical_attribute(tmp_path):
    """The model exposes `active_runner_env`. Old `code_runner_env` is a
    serialization alias only (no Python attribute by that name)."""
    cfg = _config(tmp_path, active_runner_env={"FOO": "bar"})
    assert cfg.active_runner_env == {"FOO": "bar"}
    # The old name is a Pydantic validation alias for loading; it is NOT a
    # Python attribute on the model.
    assert not hasattr(cfg, "code_runner_env")


def test_old_field_name_still_loads(tmp_path):
    """A YAML/JSON config that still uses `code_runner_env` must continue
    to populate `active_runner_env`. This is the migration safety net."""
    cfg = HarnessConfig.model_validate({
        "project_name": "t",
        "brief": "b",
        "output_dir": str(tmp_path),
        "orchestration_mode": "runner",
        "code_runner": "subprocess",
        "code_runner_env": {"OLD_NAME_KEY": "still-here"},
    })
    assert cfg.active_runner_env == {"OLD_NAME_KEY": "still-here"}


def test_serialization_uses_new_name(tmp_path):
    """Round-trip: dump uses `active_runner_env`."""
    cfg = _config(tmp_path, active_runner_env={"FOO": "bar"})
    dumped = cfg.model_dump(by_alias=True)
    assert "active_runner_env" in dumped
    # The serialization alias is the only place where the new name appears
    # in the dumped JSON.
    assert dumped["active_runner_env"] == {"FOO": "bar"}


# ── Merge semantics: profile env layered over base env ──────────────────────

def test_config_for_profile_merges_profile_over_base(tmp_path):
    base = _config(tmp_path, active_runner_env={
        "BASE_ONLY": "keep-me",
        "OVERRIDDEN": "base-value",
    })
    profile = RunnerProfile(
        name="openrouter",
        runner="subprocess",
        env={
            "OVERRIDDEN": "profile-wins",
            "PROFILE_ONLY": "from-profile",
        },
    )

    merged_cfg = config_for_profile(base, profile)
    env = merged_cfg.active_runner_env

    assert env["BASE_ONLY"] == "keep-me", "base keys must survive when profile doesn't override"
    assert env["OVERRIDDEN"] == "profile-wins", "profile must win on collision"
    assert env["PROFILE_ONLY"] == "from-profile", "profile-only keys must be added"


def test_config_for_profile_with_empty_base(tmp_path):
    """No base env → result is the profile env."""
    base = _config(tmp_path)
    profile = RunnerProfile(
        name="p",
        runner="subprocess",
        env={"K": "v"},
    )
    merged = config_for_profile(base, profile)
    assert merged.active_runner_env == {"K": "v"}


def test_config_for_profile_with_empty_profile_env(tmp_path):
    """Profile with no env → base survives unchanged."""
    base = _config(tmp_path, active_runner_env={"BASE": "kept"})
    profile = RunnerProfile(name="p", runner="subprocess")  # no env
    merged = config_for_profile(base, profile)
    assert merged.active_runner_env == {"BASE": "kept"}


def test_config_for_profile_does_not_mutate_base(tmp_path):
    """Sanity: building a per-call config must not bleed into the source config."""
    base = _config(tmp_path, active_runner_env={"BASE": "kept"})
    profile = RunnerProfile(
        name="p",
        runner="subprocess",
        env={"BASE": "leaked-if-mutated"},
    )
    config_for_profile(base, profile)
    assert base.active_runner_env == {"BASE": "kept"}


# ── Runner reads from the merged env ─────────────────────────────────────────

def test_subprocess_runner_picks_up_merged_env(tmp_path, monkeypatch):
    """End-to-end: a profile-projected config flows into subprocess.run env."""
    from unittest.mock import MagicMock
    from harness.runners.subprocess_runner import SubprocessRunner

    base = _config(tmp_path, active_runner_env={"BASE": "from-base"})
    profile = RunnerProfile(
        name="or",
        runner="subprocess",
        env={"PROFILE": "from-profile"},
    )
    cfg = config_for_profile(base, profile)

    captured: dict = {}
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ok"
    mock_result.stderr = ""

    def fake_run(args, *a, **k):
        captured["env"] = k.get("env")
        return mock_result

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.run", fake_run)

    SubprocessRunner(cfg).implement("prompt", cwd="/tmp")
    env = captured["env"]
    assert env["BASE"] == "from-base"
    assert env["PROFILE"] == "from-profile"
