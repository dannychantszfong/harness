"""Git/GitHub helpers for generated output projects.

Harness itself ignores `output/`. Each generated project inside output is an
independent git repository, optionally linked to its own GitHub repo.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.config import HarnessConfig


@dataclass
class ProjectGitSyncResult:
    ok: bool
    message: str
    skipped: bool = False


def project_git_enabled(config: HarnessConfig) -> bool:
    return bool(
        config.project_git_push
        and (config.project_git_remote or config.project_github_repo)
    )


def sync_project_git(config: HarnessConfig, reason: str = "workflow") -> ProjectGitSyncResult:
    """Push the output project repo to its configured remote.

    This function never touches the Harness repository. It always runs inside
    config.output_dir, which is expected to be its own git repository.
    """
    if not project_git_enabled(config):
        return ProjectGitSyncResult(ok=True, skipped=True, message="project git push disabled")

    project_dir = Path(config.output_dir)
    if not project_dir.exists():
        return ProjectGitSyncResult(ok=False, message=f"output dir does not exist: {project_dir}")

    configured_branch = config.project_git_branch or "main"
    try:
        branch = _ensure_repo(project_dir, configured_branch)
        remote_url = _resolve_remote(config, project_dir)
        _ensure_remote(project_dir, remote_url)

        head = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=project_dir, check=False)
        if head.returncode != 0:
            return ProjectGitSyncResult(ok=True, skipped=True, message="no commits to push yet")

        _run(["git", "push", "-u", "origin", branch], cwd=project_dir)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return ProjectGitSyncResult(ok=False, message=detail)
    except RuntimeError as exc:
        return ProjectGitSyncResult(ok=False, message=str(exc))

    return ProjectGitSyncResult(
        ok=True,
        message=f"pushed {project_dir} to origin/{branch} after {reason}",
    )


def _ensure_repo(project_dir: Path, configured_branch: str) -> str:
    if not (project_dir / ".git").exists():
        _run(["git", "init"], cwd=project_dir)
        _run(["git", "branch", "-M", configured_branch], cwd=project_dir, check=False)
        return configured_branch

    current = _run(["git", "branch", "--show-current"], cwd=project_dir, check=False)
    branch = current.stdout.strip() if current.returncode == 0 else ""
    return branch or configured_branch


def _resolve_remote(config: HarnessConfig, project_dir: Path) -> str:
    if config.project_git_remote:
        return config.project_git_remote
    if not config.project_github_repo:
        raise RuntimeError("project_git_push is enabled but no remote or GitHub repo is configured")

    _ensure_github_repo(config, project_dir)
    return f"https://github.com/{config.project_github_repo}.git"


def _ensure_github_repo(config: HarnessConfig, project_dir: Path) -> None:
    if not shutil.which("gh"):
        raise RuntimeError(
            "`gh` GitHub CLI is required to create/check project_github_repo. "
            "Install gh, run `gh auth login`, or set project_git_remote to an existing repo URL."
        )

    repo = (config.project_github_repo or "").strip()
    if repo.count("/") != 1 or any(not part for part in repo.split("/")):
        raise RuntimeError("project_github_repo must use owner/repo format")

    view = _run(["gh", "repo", "view", repo], cwd=project_dir, check=False)
    if view.returncode == 0:
        return

    visibility = "--private" if config.project_github_private else "--public"
    try:
        _run(["gh", "repo", "create", repo, visibility, "--confirm"], cwd=project_dir)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").lower()
        if "unknown flag" not in detail or "confirm" not in detail:
            raise
        _run(["gh", "repo", "create", repo, visibility], cwd=project_dir)


def _ensure_remote(project_dir: Path, remote_url: str) -> None:
    current = _run(["git", "remote", "get-url", "origin"], cwd=project_dir, check=False)
    if current.returncode == 0:
        if current.stdout.strip() != remote_url:
            _run(["git", "remote", "set-url", "origin", remote_url], cwd=project_dir)
        return
    _run(["git", "remote", "add", "origin", remote_url], cwd=project_dir)


def _run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
