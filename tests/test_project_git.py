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

    def fake_run(args, cwd, capture_output=True, text=True, check=True):
        calls.append(args)
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


def test_sync_project_git_reports_missing_gh(tmp_path, monkeypatch):
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

    result = sync_project_git(config)

    assert result.ok is False
    assert "GitHub CLI" in result.message
