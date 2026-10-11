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

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.config import HarnessConfig


# Runtime files Harness writes into the project directory that must never be
# committed (auto_resume wrappers carry absolute paths; the log is noise).
PROJECT_GITIGNORE_ENTRIES: tuple[str, ...] = (
    ".auto_resume.sh",
    ".auto_resume.ps1",
    "auto_resume.log",
    ".harness/run.lock",
)


def ensure_project_gitignore(project_dir: Path) -> bool:
    """Append missing Harness runtime entries to <project>/.gitignore.

    Idempotent; existing content is kept. Returns True if the file changed.
    """
    path = Path(project_dir) / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return False
    present = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry in PROJECT_GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return False
    block = ""
    if existing and not existing.endswith("\n"):
        block += "\n"
    if "# Harness runtime files" not in present:
        block += "# Harness runtime files\n"
    block += "\n".join(missing) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError:
        return False
    return True


# ── Local recovery points (C-02 / ADR-015) ───────────────────────────────────
#
# Harness owns one commit per passing feature and, when a feature exhausts its
# budget, preserves the attempt on a branch before reverting to the feature's
# base commit. All helpers are no-ops (returning None/False) outside a repo.

_FALLBACK_IDENTITY = {
    "GIT_AUTHOR_NAME": "Agent Harness",
    "GIT_AUTHOR_EMAIL": "harness@localhost",
    "GIT_COMMITTER_NAME": "Agent Harness",
    "GIT_COMMITTER_EMAIL": "harness@localhost",
}


def is_git_repo(project_dir: Path) -> bool:
    """True only if project_dir is the *root* of its own work tree.

    Being somewhere inside a work tree is not enough: output/ lives inside
    the Harness checkout, and a project whose `git init` failed must never
    have commits — let alone `reset --hard` — applied to the parent repo.
    """
    if shutil.which("git") is None or not Path(project_dir).is_dir():
        return False
    proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=project_dir, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    return Path(proc.stdout.strip()).resolve() == Path(project_dir).resolve()


def git_head(project_dir: Path) -> str | None:
    """Current HEAD sha of the project's own repo, or None."""
    if not is_git_repo(project_dir):
        return None
    proc = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=project_dir, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_dirty(project_dir: Path) -> bool:
    proc = _run(["git", "status", "--porcelain"], cwd=project_dir, check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def dirty_paths(project_dir: Path) -> list[str]:
    """Paths with uncommitted changes, untracked files listed one by one."""
    proc = _run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=project_dir,
        check=False,
    )
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    entries = proc.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            i += 1  # the next entry is the rename/copy source
    return paths


def commit_all(project_dir: Path, message: str) -> str | None:
    """`git add -A && git commit -m message` if the tree is dirty.

    Returns HEAD afterwards (the new commit, or the existing HEAD when there
    was nothing to commit); None when this is not a usable repo.
    """
    if not is_git_repo(project_dir):
        return None
    if is_dirty(project_dir):
        absorb_nested_repos(project_dir)
        _run(["git", "add", "-A"], cwd=project_dir)
        _run(["git", "commit", "-m", message], cwd=project_dir, env=_identity_env(project_dir))
    return git_head(project_dir)


def nested_repos(project_dir: Path) -> list[str]:
    """Repositories below the project root that `git add -A` would record as
    gitlinks: untracked directories with their own .git (git lists those
    with a trailing slash), and gitlinks already staged — minus real
    submodules declared in .gitmodules."""
    others = _run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=project_dir, check=False)
    found = [p.rstrip("/") for p in others.stdout.split("\0") if p.endswith("/")]
    staged = _run(["git", "ls-files", "--stage", "-z"], cwd=project_dir, check=False)
    for entry in staged.stdout.split("\0"):
        if entry.startswith("160000 ") and "\t" in entry:
            found.append(entry.split("\t", 1)[1])
    submodules = _submodule_paths(project_dir)
    return sorted({p for p in found if p and p not in submodules})


def absorb_nested_repos(project_dir: Path) -> list[tuple[str, str]]:
    """Turn nested repositories into plain directories before committing.

    Scaffolders (`rails new`, some templates) run `git init` in a
    subdirectory. `git add -A` then records a gitlink with no .gitmodules
    (clones break), and `git reset --hard` cannot remove the nested repo.
    The nested .git is moved — not deleted — into the project's own git
    directory (<git-dir>/harness/nested-repos/), so its history survives
    and the files are committed as ordinary files. Returns
    [(path, "absorbed" | "excluded")]; "excluded" (kept out of git via
    .git/info/exclude) when the move failed.
    """
    results: list[tuple[str, str]] = []
    paths = nested_repos(project_dir)
    if not paths:
        return results
    store = _git_dir(project_dir) / "harness" / "nested-repos"
    for rel in paths:
        _run(["git", "rm", "--cached", "-q", "--ignore-unmatch", "--", rel], cwd=project_dir, check=False)
        inner = Path(project_dir) / rel / ".git"
        if inner.exists():
            store.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in rel) or "repo"
            dest = store / safe
            n = 1
            while dest.exists():
                n += 1
                dest = store / f"{safe}-{n}"
            try:
                shutil.move(str(inner), str(dest))
            except OSError:
                _append_exclude(project_dir, ["/" + _escape_gitignore(rel) + "/"])
                results.append((rel, "excluded"))
                continue
        results.append((rel, "absorbed"))
    return results


def _submodule_paths(project_dir: Path) -> set[str]:
    if not (Path(project_dir) / ".gitmodules").is_file():
        return set()
    proc = _run(
        ["git", "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        cwd=project_dir,
        check=False,
    )
    return {line.split(" ", 1)[1].strip() for line in proc.stdout.splitlines() if " " in line}


def _git_dir(project_dir: Path) -> Path:
    rel = _run(["git", "rev-parse", "--git-dir"], cwd=project_dir).stdout.strip()
    path = Path(rel)
    return path if path.is_absolute() else Path(project_dir) / path


def commit_paths(project_dir: Path, paths: list[str], message: str) -> str | None:
    """Commit only `paths` (relative to the project), if any of them changed.

    Used after a revert, where `git add -A` would sweep up whatever the
    reverted attempt left on disk.
    """
    if not is_git_repo(project_dir):
        return None
    existing = [p for p in paths if (Path(project_dir) / p).exists()]
    if existing:
        _run(["git", "add", "-A", "--", *existing], cwd=project_dir)
    staged = _run(["git", "diff", "--cached", "--quiet"], cwd=project_dir, check=False)
    if staged.returncode == 1:
        _run(["git", "commit", "-m", message], cwd=project_dir, env=_identity_env(project_dir))
    return git_head(project_dir)


def create_branch(project_dir: Path, name: str, ref: str = "HEAD") -> None:
    _run(["git", "branch", "-f", name, ref], cwd=project_dir)


def reset_hard(project_dir: Path, ref: str) -> None:
    _run(["git", "reset", "--hard", ref], cwd=project_dir)


def is_ancestor(project_dir: Path, ancestor: str, descendant: str) -> bool:
    proc = _run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=project_dir,
        check=False,
    )
    return proc.returncode == 0


def ignored_untracked_paths(project_dir: Path) -> list[str]:
    """Untracked paths the current .gitignore rules exclude (dirs collapsed)."""
    proc = _run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
        cwd=project_dir,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.split("\0") if p]


def keep_excluded(project_dir: Path, paths: list[str]) -> list[str]:
    """Keep files out of git after a revert changed the ignore rules.

    `git reset --hard` restores the base .gitignore but leaves ignored,
    untracked files (node_modules/, .env, build output) on disk; without
    this they would be swept into the next `git add -A` and pushed. Paths
    no longer ignored are added to .git/info/exclude — local, never
    committed, nothing deleted. Returns the paths added.
    """
    added: list[str] = []
    for path in paths:
        if not (Path(project_dir) / path.rstrip("/")).exists():
            continue
        check = _run(["git", "check-ignore", "-q", "--", path], cwd=project_dir, check=False)
        if check.returncode == 1:  # 1 = not ignored any more
            added.append(path)
    if not added:
        return []
    _append_exclude(
        project_dir,
        ["/" + _escape_gitignore(p) for p in added],
        comment="Left behind by an exhausted Harness attempt",
    )
    return added


def _append_exclude(project_dir: Path, patterns: list[str], comment: str = "Added by Harness") -> None:
    """Append patterns to .git/info/exclude (local; never committed)."""
    exclude_rel = _run(
        ["git", "rev-parse", "--git-path", "info/exclude"], cwd=project_dir
    ).stdout.strip()
    exclude = Path(project_dir) / exclude_rel if not Path(exclude_rel).is_absolute() else Path(exclude_rel)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    with open(exclude, "a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join([f"# {comment}", *patterns]) + "\n")


def _escape_gitignore(path: str) -> str:
    escaped = "".join("\\" + c if c in "\\*?[" else c for c in path)
    return escaped


def _identity_env(project_dir: Path) -> dict[str, str] | None:
    """Fallback author/committer when git has no identity configured.

    Without it `git commit` fails on fresh machines and CI runners and the
    recovery point silently never happens.
    """
    email = _run(["git", "config", "user.email"], cwd=project_dir, check=False)
    if email.returncode == 0 and email.stdout.strip():
        return None
    env = os.environ.copy()
    for key, value in _FALLBACK_IDENTITY.items():
        env.setdefault(key, value)
    return env


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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    extra = {"env": env} if env is not None else {}
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        **extra,
    )
