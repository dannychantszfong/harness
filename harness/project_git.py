"""Git/GitHub helpers for generated output projects.

Harness itself ignores `output/`. Each generated project inside output is an
independent git repository, optionally linked to its own GitHub repo.

Two failure modes are separated on purpose:

  • `ProjectGitConfigurationError` — the host is not actually ready for
    GitHub sync (gh missing, or installed but unauthenticated). This is
    a setup-time problem; `harness setup` is expected to catch it before
    any project ever runs. The runtime check exists only as defense in
    depth and fails loudly so the user fixes their environment, rather
    than silently degrading.

  • `ProjectGitSyncResult(ok=False, …)` — a transient runtime issue with
    the actual sync: network blip, push rejected, remote changed by
    someone else. These are not configuration problems and shouldn't
    abort the harness run; they're surfaced as warnings.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.config import HarnessConfig


class ProjectGitConfigurationError(Exception):
    """Raised when the host's GitHub-sync prerequisites aren't satisfied.

    Distinct from a transient sync failure (network, push rejected, etc.)
    so the orchestrator can hard-fail on configuration problems while
    keeping run-time hiccups non-fatal.
    """


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
    except ProjectGitConfigurationError:
        # Configuration errors must escape — they signal a missing/broken
        # tool that `harness setup` should have caught. The orchestrator
        # surfaces these as a hard failure, not a yellow warning panel.
        raise
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
    _require_gh_ready()

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


def _require_gh_ready() -> None:
    """Hard-fail when `gh` is missing or not authenticated.

    GitHub sync is treated as required infrastructure: `harness setup`
    runs the same check up front (see harness/preflight.py). This
    function exists as defense in depth in case the user's environment
    changed between setup and run, or sync was enabled on a project
    config that bypassed setup.
    """
    if shutil.which("gh") is None:
        raise ProjectGitConfigurationError(
            "`gh` GitHub CLI is required for project GitHub sync but was not "
            "found on PATH. Run `harness setup --auto-install` (macOS/Windows) "
            "or install gh manually (https://cli.github.com/), then re-run "
            "`harness setup` to verify. To opt out of GitHub sync entirely, "
            "set `project_git_push: false` in the project config."
        )

    auth = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    if auth.returncode != 0:
        detail = (auth.stderr or auth.stdout or "").strip()
        raise ProjectGitConfigurationError(
            "`gh` is installed but not authenticated, so project GitHub sync "
            "cannot proceed.\n"
            f"gh auth status: {detail or '(no output)'}\n\n"
            "Fix:\n"
            "  gh auth login\n"
            "Then re-run `harness setup` to confirm the environment is ready."
        )


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
