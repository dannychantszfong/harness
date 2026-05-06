"""Cross-platform runtime tests — generated projects on macOS, Linux, Windows.

These tests pin the platform-aware behavior introduced when the
initializer was rewritten to emit `init.sh` / `init.ps1` / `init.bat`
based on host platform (or explicit override). Mocked via
`platform.system` so the same test suite runs the same way on every CI.

Two layers, both checked:

  • The harness's own Python runtime (uses pathlib + subprocess args
    everywhere, no shell=True, etc.) — sanity-checked indirectly.
  • The script flavor of the *generated* project — checked directly
    via `effective_init_script_type`, the chmod policy, and the
    initializer's prompt to the agent.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.config import HarnessConfig
from harness.agents.initializer import InitializerAgent


# ── HarnessConfig: effective_init_script_type / effective_init_script ───────

def _config(tmp_path: Path, **kwargs) -> HarnessConfig:
    return HarnessConfig(
        project_name="t",
        brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
        code_runner="subprocess",
        **kwargs,
    )


def test_default_on_macos_is_bash(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    cfg = _config(tmp_path)
    assert cfg.effective_init_script_type == "bash"
    assert cfg.effective_init_script == "init.sh"


def test_default_on_linux_is_bash(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    cfg = _config(tmp_path)
    assert cfg.effective_init_script_type == "bash"
    assert cfg.effective_init_script == "init.sh"


def test_default_on_windows_is_powershell(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cfg = _config(tmp_path)
    assert cfg.effective_init_script_type == "powershell"
    assert cfg.effective_init_script == "init.ps1"


def test_explicit_type_overrides_platform(tmp_path, monkeypatch):
    """A user on Windows can target bash if they want (e.g. WSL)."""
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cfg = _config(tmp_path, init_script_type="bash")
    assert cfg.effective_init_script_type == "bash"
    assert cfg.effective_init_script == "init.sh"


def test_explicit_filename_drives_type_when_type_unset(tmp_path, monkeypatch):
    """Backward-compat: existing configs with `init_script: 'init.sh'`
    continue to behave as bash regardless of host platform."""
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cfg = _config(tmp_path, init_script="init.sh")
    assert cfg.effective_init_script_type == "bash"
    assert cfg.effective_init_script == "init.sh"


def test_cmd_type_picks_bat_filename(tmp_path):
    cfg = _config(tmp_path, init_script_type="cmd")
    assert cfg.effective_init_script == "init.bat"
    assert cfg.startup_command_for_platform == "init.bat"


def test_powershell_type_picks_correct_startup_command(tmp_path):
    cfg = _config(tmp_path, init_script_type="powershell")
    cmd = cfg.startup_command_for_platform
    assert "powershell" in cmd
    assert "ExecutionPolicy" in cmd
    assert "init.ps1" in cmd


def test_bash_type_picks_correct_startup_command(tmp_path):
    cfg = _config(tmp_path, init_script_type="bash")
    assert cfg.startup_command_for_platform == "bash init.sh"


def test_explicit_startup_command_wins(tmp_path):
    """If user supplies `startup_command`, the property must just return it."""
    cfg = _config(
        tmp_path, init_script_type="powershell", startup_command="npm start"
    )
    assert cfg.startup_command_for_platform == "npm start"


# ── Initializer: prompts the agent for the right script flavor ──────────────

def _fake_runner_writing(features_path: Path, init_path: Path, init_content: str):
    """Build a runner mock that writes the requested files to disk."""
    import json as _json

    def implement(prompt, cwd, timeout_seconds=600):
        features_path.write_text(_json.dumps([
            {"id": "f1", "name": "x", "description": "y", "priority": 0}
        ]))
        init_path.write_text(init_content)
        return MagicMock(
            success=True, output="INIT_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
            rate_limited=False,
        )

    runner = MagicMock()
    runner.implement.side_effect = implement
    return runner


def test_initializer_prompt_mentions_powershell_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cfg = _config(tmp_path)

    captured: dict[str, str] = {}

    def fake_implement(prompt, cwd, timeout_seconds=600):
        captured["prompt"] = prompt
        (tmp_path / "features.json").write_text(
            '[{"id":"f1","name":"x","description":"y","priority":0}]'
        )
        (tmp_path / "init.ps1").write_text("Write-Host 'ok'\n")
        return MagicMock(
            success=True, output="INIT_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
            rate_limited=False,
        )

    runner = MagicMock()
    runner.implement.side_effect = fake_implement
    InitializerAgent(cfg, runner=runner).run(brief="build a calendar")

    prompt = captured["prompt"]
    assert "init.ps1" in prompt
    assert "powershell" in prompt.lower()


def test_initializer_prompt_mentions_bash_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    cfg = _config(tmp_path)

    captured: dict[str, str] = {}

    def fake_implement(prompt, cwd, timeout_seconds=600):
        captured["prompt"] = prompt
        (tmp_path / "features.json").write_text(
            '[{"id":"f1","name":"x","description":"y","priority":0}]'
        )
        (tmp_path / "init.sh").write_text("#!/bin/bash\necho ok\n")
        return MagicMock(
            success=True, output="INIT_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
            rate_limited=False,
        )

    runner = MagicMock()
    runner.implement.side_effect = fake_implement
    InitializerAgent(cfg, runner=runner).run(brief="build a calendar")

    prompt = captured["prompt"]
    assert "init.sh" in prompt
    assert "bash" in prompt.lower()


def test_initializer_skips_chmod_for_powershell(tmp_path, monkeypatch):
    """chmod 0o755 is bash-only; PowerShell scripts shouldn't have their
    mode mutated (no-op on Windows but cleanest to skip explicitly)."""
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cfg = _config(tmp_path)

    chmod_calls: list[int] = []
    real_chmod = Path.chmod

    def tracking_chmod(self, mode):
        chmod_calls.append(mode)
        return real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", tracking_chmod)

    def fake_implement(prompt, cwd, timeout_seconds=600):
        (tmp_path / "features.json").write_text(
            '[{"id":"f1","name":"x","description":"y","priority":0}]'
        )
        (tmp_path / "init.ps1").write_text("Write-Host 'ok'\n")
        return MagicMock(
            success=True, output="INIT_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
            rate_limited=False,
        )

    runner = MagicMock()
    runner.implement.side_effect = fake_implement
    InitializerAgent(cfg, runner=runner).run(brief="x")

    # No chmod 0o755 call (bash-only). Other chmod calls (e.g. by git or
    # tempfile) may exist; we only assert the bash-specific 0o755 didn't fire.
    assert 0o755 not in chmod_calls


def test_initializer_falls_back_to_correct_default_per_type(tmp_path, monkeypatch):
    """When the runner doesn't write the script, the initializer must pick
    the platform-correct default content."""
    monkeypatch.setattr("platform.system", lambda: "Windows")
    cfg = _config(tmp_path)

    def fake_implement(prompt, cwd, timeout_seconds=600):
        # Runner writes only features.json, not the script
        (tmp_path / "features.json").write_text(
            '[{"id":"f1","name":"x","description":"y","priority":0}]'
        )
        return MagicMock(
            success=True, output="INIT_DONE", error=None,
            rate_limit_reset_at=None, input_tokens=None, output_tokens=None,
            rate_limited=False,
        )

    runner = MagicMock()
    runner.implement.side_effect = fake_implement
    InitializerAgent(cfg, runner=runner).run(brief="x")

    init_path = tmp_path / "init.ps1"
    assert init_path.exists()
    body = init_path.read_text()
    # PowerShell idiom — no #!/bin/bash, uses Write-Host
    assert body.startswith("Write-Host") or "Write-Host" in body
    assert "#!/bin/bash" not in body
