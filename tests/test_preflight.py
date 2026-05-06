"""Tests for harness/preflight.py and the setup-blocking behavior in CLI.

External tools (`gh`, `claude`, `codex`) are mocked via `shutil.which`
and `subprocess.run`. Auto-install branches use lambdas, never shell out.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli import main
from harness import preflight


# ── unit tests for the preflight module ──────────────────────────────────────

def test_check_passes_when_binary_present_and_no_auth_probe(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    check = preflight.ToolCheck(name="codex", purpose="codex runner")
    [result] = preflight.run_preflight([check])
    assert result.ok is True
    assert result.binary_present is True
    assert result.error is None


def test_check_fails_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    check = preflight.ToolCheck(name="claude", purpose="claude code")
    [result] = preflight.run_preflight([check])
    assert result.ok is False
    assert "claude" in result.error
    assert result.binary_present is False


def test_check_with_auth_probe_failure(monkeypatch):
    """gh installed but not authed → not ok, error mentions auth."""
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/homebrew/bin/{name}")
    check = preflight.ToolCheck(
        name="gh",
        purpose="GitHub sync",
        auth_probe=lambda: preflight.AuthProbeResult(ok=False, detail="run gh auth login"),
    )
    [result] = preflight.run_preflight([check])
    assert result.ok is False
    assert "not authenticated" in result.error
    assert "gh auth login" in result.auth_detail


def test_auto_install_runs_when_enabled_and_binary_missing(monkeypatch):
    calls = []
    def fake_which(name):
        # Missing on first call, present after install
        return None if not calls else f"/usr/local/bin/{name}"
    monkeypatch.setattr("shutil.which", fake_which)

    def fake_install():
        calls.append("installed")
        return True

    check = preflight.ToolCheck(
        name="gh",
        purpose="GitHub sync",
        auto_install=fake_install,
    )
    [result] = preflight.run_preflight([check], auto_install=True)
    assert result.ok is True
    assert result.installed_now is True
    assert calls == ["installed"]


def test_auto_install_off_by_default(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    install_called = []
    check = preflight.ToolCheck(
        name="gh",
        purpose="GitHub sync",
        auto_install=lambda: install_called.append(True) or True,
    )
    [result] = preflight.run_preflight([check])  # default auto_install=False
    assert result.ok is False
    assert install_called == []


def test_runner_checks_for_includes_only_referenced_clis():
    """If profiles only use codex, no claude check is added (and vice versa)."""
    only_codex = preflight.runner_checks_for({"codex"})
    only_claude = preflight.runner_checks_for({"subprocess", "sdk"})
    both = preflight.runner_checks_for({"subprocess", "codex"})

    assert {c.name for c in only_codex} == {"codex"}
    assert {c.name for c in only_claude} == {"claude"}
    assert {c.name for c in both} == {"claude", "codex"}


def test_gh_auth_probe_returns_ok_on_zero_exit(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    proc = MagicMock(returncode=0, stdout="Logged in as octocat", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: proc)
    result = preflight.gh_auth_probe()
    assert result.ok is True
    assert "octocat" in result.detail


def test_gh_auth_probe_returns_error_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    proc = MagicMock(returncode=1, stdout="", stderr="You are not logged in.")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: proc)
    result = preflight.gh_auth_probe()
    assert result.ok is False
    assert "logged in" in result.detail


def test_install_gh_macos_requires_homebrew(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert preflight.install_gh_macos() is False


def test_install_gh_windows_requires_winget(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert preflight.install_gh_windows() is False


def test_manual_hint_per_platform(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    hint = preflight.manual_hint_for(preflight.gh_check())
    assert "dnf" in hint or "Debian" in hint or "Linux" in hint or "linux" in hint.lower()

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert "brew" in preflight.manual_hint_for(preflight.gh_check())

    monkeypatch.setattr("platform.system", lambda: "Windows")
    assert "winget" in preflight.manual_hint_for(preflight.gh_check())


# ── CLI: harness setup must block when gh is missing ─────────────────────────

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_setup_blocks_when_gh_missing(runner, tmp_path, monkeypatch):
    """Setup must exit non-zero and not save the file when gh isn't installed."""
    setup_path = tmp_path / "setup.json"

    def which_no_gh(name):
        return None if name == "gh" else f"/usr/bin/{name}"

    monkeypatch.setattr("shutil.which", which_no_gh)

    result = runner.invoke(main, [
        "setup",
        "--config", str(setup_path),
        "--profile", "claude:subprocess::subscription",
        "--generator-order", "claude",
    ])

    assert result.exit_code != 0
    assert not setup_path.exists()
    assert "gh" in result.output
    assert "Setup not saved" in result.output


def test_setup_blocks_when_gh_unauthenticated(runner, tmp_path, monkeypatch):
    """gh on PATH but not authed → still blocks."""
    setup_path = tmp_path / "setup.json"
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(returncode=1, stdout="", stderr="not logged in"),
    )

    result = runner.invoke(main, [
        "setup",
        "--config", str(setup_path),
        "--profile", "claude:subprocess::subscription",
    ])

    assert result.exit_code != 0
    assert not setup_path.exists()
    assert "gh auth login" in result.output


def test_setup_skip_preflight_bypasses_checks(runner, tmp_path, monkeypatch):
    """Escape hatch — `--skip-preflight` saves the file without probing."""
    setup_path = tmp_path / "setup.json"
    monkeypatch.setattr("shutil.which", lambda _: None)

    result = runner.invoke(main, [
        "setup",
        "--config", str(setup_path),
        "--profile", "claude:subprocess::subscription",
        "--skip-preflight",
    ])

    assert result.exit_code == 0, result.output
    assert setup_path.exists()


def test_setup_succeeds_when_all_tools_present(runner, tmp_path, monkeypatch):
    """gh + claude installed, gh authed → setup saves cleanly."""
    setup_path = tmp_path / "setup.json"
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stdout="Logged in as foo", stderr=""),
    )

    result = runner.invoke(main, [
        "setup",
        "--config", str(setup_path),
        "--profile", "claude:subprocess::subscription",
    ])

    assert result.exit_code == 0, result.output
    assert setup_path.exists()
    assert "All required tools present" in result.output


def test_setup_only_checks_clis_for_profiles_in_use(runner, tmp_path, monkeypatch):
    """Codex-only setup must not require the `claude` binary."""
    setup_path = tmp_path / "setup.json"

    def which_no_claude(name):
        if name == "claude":
            return None
        return f"/usr/local/bin/{name}"

    monkeypatch.setattr("shutil.which", which_no_claude)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stdout="ok", stderr=""),
    )

    result = runner.invoke(main, [
        "setup",
        "--config", str(setup_path),
        "--profile", "codex:codex::subscription",
    ])

    assert result.exit_code == 0, result.output
    assert setup_path.exists()
