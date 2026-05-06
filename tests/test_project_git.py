"""Tests for per-output-project Git/GitHub sync helpers."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.config import HarnessConfig
from harness.project_git import project_git_enabled, sync_project_git


@pytest.fixture
def cfg(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="GitMe",
        brief="b",
        output_dir=str(tmp_path),
        project_git_push=True,
        project_git_branch="main",
        project_git_remote="git@github.com:owner/gitme.git",
    )


def test_project_git_disabled_without_remote(tmp_path):
    config = HarnessConfig(project_name="NoPush", brief="b", output_dir=str(tmp_path))
    assert project_git_enabled(config) is False
    result = sync_project_git(config)
    assert result.skipped is True


def test_sync_project_git_pushes_configured_remote(cfg, monkeypatch):
    calls = []

    def fake_run(args, cwd, capture_output=True, text=True, check=True):
        calls.append(args)
        if args[:3] == ["git", "remote", "get-url"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_project_git(cfg, reason="feature f1")

    assert result.ok is True
    assert ["git", "init"] in calls
    assert ["git", "remote", "add", "origin", "git@github.com:owner/gitme.git"] in calls
    assert ["git", "push", "-u", "origin", "main"] in calls


def test_sync_project_git_keeps_existing_repo_branch(cfg, monkeypatch):
    (Path(cfg.output_dir) / ".git").mkdir()
    calls = []

    def fake_run(args, cwd, capture_output=True, text=True, check=True):
        calls.append(args)
        if args[:3] == ["git", "branch", "--show-current"]:
            return MagicMock(returncode=0, stdout="develop\n", stderr="")
        if args[:3] == ["git", "remote", "get-url"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_project_git(cfg)

    assert result.ok is True
    assert ["git", "branch", "-M", "main"] not in calls
    assert ["git", "push", "-u", "origin", "develop"] in calls


def test_sync_project_git_creates_github_repo_when_needed(tmp_path, monkeypatch):
    config = HarnessConfig(
        project_name="GitHubMe",
        brief="b",
        output_dir=str(tmp_path),
        project_git_push=True,
        project_github_repo="owner/githubme",
        project_github_private=False,
    )
    calls = []

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, *a, **k):
        calls.append(args)
        # gh auth status (no cwd arg) — `_require_gh_ready` calls this
        if args[:3] == ["gh", "auth", "status"]:
            return MagicMock(returncode=0, stdout="Logged in", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        if args[:3] == ["git", "remote", "get-url"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_project_git(config, reason="initialization")

    assert result.ok is True
    assert ["gh", "repo", "create", "owner/githubme", "--public", "--confirm"] in calls
    assert ["git", "remote", "add", "origin", "https://github.com/owner/githubme.git"] in calls


# ── Hard-failure contract for the gh-required path ──────────────────────────

def test_sync_raises_configuration_error_when_gh_missing(tmp_path, monkeypatch):
    """If sync needs gh (project_github_repo set, no remote URL) and gh is
    not on PATH, sync_project_git must raise ProjectGitConfigurationError —
    not return ok=False. `harness setup` is supposed to catch this."""
    from harness.project_git import ProjectGitConfigurationError

    config = HarnessConfig(
        project_name="GitHubMe",
        brief="b",
        output_dir=str(tmp_path),
        project_git_push=True,
        project_github_repo="owner/githubme",
    )
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: MagicMock(returncode=0, stdout="ok", stderr=""),
    )

    with pytest.raises(ProjectGitConfigurationError) as exc:
        sync_project_git(config)

    msg = str(exc.value)
    assert "gh" in msg.lower()
    assert "harness setup" in msg.lower()


def test_sync_raises_configuration_error_when_gh_unauthenticated(tmp_path, monkeypatch):
    """gh installed but `gh auth status` non-zero → fail hard with a
    pointer to `gh auth login` and `harness setup`."""
    from harness.project_git import ProjectGitConfigurationError

    config = HarnessConfig(
        project_name="GitHubMe",
        brief="b",
        output_dir=str(tmp_path),
        project_git_push=True,
        project_github_repo="owner/githubme",
    )
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/homebrew/bin/{name}")

    def fake_run(args, *a, **k):
        if args[:3] == ["gh", "auth", "status"]:
            return MagicMock(returncode=1, stdout="", stderr="You are not logged in.")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ProjectGitConfigurationError) as exc:
        sync_project_git(config)

    msg = str(exc.value)
    assert "gh auth login" in msg
    assert "harness setup" in msg.lower()


def test_orchestrator_reraises_configuration_error(tmp_path, monkeypatch):
    """The orchestrator's `_sync_project_git` must propagate
    `ProjectGitConfigurationError` so the run aborts cleanly.
    Transient sync failures (CalledProcessError translated to
    `ok=False`) stay non-fatal — only configuration errors hard-fail.
    """
    from harness.orchestrator import Orchestrator
    from harness.project_git import ProjectGitConfigurationError

    config = HarnessConfig(
        project_name="GitHubMe",
        brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
        code_runner="subprocess",
        project_git_push=True,
        project_github_repo="owner/githubme",
    )
    config.save_file(tmp_path / "harness_config.json")

    fake_runner = MagicMock()
    fake_runner.preflight.return_value = MagicMock(
        ok=True, summary="stub", details="stub"
    )
    monkeypatch.setattr(
        "harness.orchestrator.create_runner", lambda *a, **k: fake_runner
    )

    def boom(_config, reason="workflow"):
        raise ProjectGitConfigurationError("gh missing — run harness setup")

    monkeypatch.setattr("harness.orchestrator.sync_project_git", boom)

    orch = Orchestrator(config)
    with pytest.raises(ProjectGitConfigurationError):
        orch._sync_project_git(reason="initialization")


def test_sync_with_explicit_remote_does_not_require_gh(tmp_path, monkeypatch):
    """When project_git_remote (a URL) is set and project_github_repo is NOT,
    gh is not actually needed — sync must work without it."""
    config = HarnessConfig(
        project_name="GitHubMe",
        brief="b",
        output_dir=str(tmp_path),
        project_git_push=True,
        project_git_remote="git@github.com:owner/repo.git",
    )
    monkeypatch.setattr("shutil.which", lambda name: None)  # nothing on PATH

    def fake_run(args, *a, **k):
        if args[:2] == ["git", "remote"] and (len(args) < 3 or args[2] != "get-url"):
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[:3] == ["git", "remote", "get-url"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        if args[:2] == ["git", "rev-parse"]:
            return MagicMock(returncode=0, stdout="abc123", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    # When _ensure_repo runs git init/branch, those subprocess calls succeed.
    # Be permissive about which.
    monkeypatch.setattr("subprocess.run", fake_run)

    # Mock _ensure_repo's branch detection by ensuring `git branch --show-current`
    # also succeeds; covered by the default path in fake_run.
    result = sync_project_git(config)

    # The push will be mocked as success too
    assert result.ok is True or result.skipped is True
