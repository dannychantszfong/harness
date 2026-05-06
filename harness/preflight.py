"""Tool-availability preflight for `harness setup` and friends.

GitHub sync is core infrastructure for this project, not optional, so
`gh` is always required at setup time. The runner profiles the user
configures determine which coding-agent CLIs (claude, codex) must also
be present.

Auto-install is attempted only via package managers we trust to be
non-destructive on the host platform:

  • macOS  → Homebrew (`brew install gh`)
  • Windows → winget (`winget install GitHub.cli`)
  • Linux   → no auto-install (too many distros, wrong package-manager
              guess can be destructive). Print copy-pasteable hints and
              block setup completion.

Coding-agent CLIs aren't auto-installable on any platform — the Claude
Code installer isn't scriptable, and `npm install -g @openai/codex`
mutates the user's global npm prefix; we don't want to do either
silently.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ToolCheck:
    """Spec for one preflight check."""
    name: str                          # bin name resolved via shutil.which
    purpose: str                       # short human label
    required: bool = True              # block setup when missing
    auth_probe: Optional[Callable[[], "AuthProbeResult"]] = None
    auto_install: Optional[Callable[[], bool]] = None
    manual_install_hint: dict[str, str] = field(default_factory=dict)


@dataclass
class AuthProbeResult:
    ok: bool
    detail: str


@dataclass
class CheckResult:
    tool: ToolCheck
    binary_present: bool
    auth_ok: bool = True              # vacuously true when no auth_probe
    auth_detail: str = ""
    installed_now: bool = False        # True if auto-install ran successfully
    error: Optional[str] = None        # human-readable problem statement

    @property
    def ok(self) -> bool:
        return self.binary_present and self.auth_ok and self.error is None


# ── Built-in checks ──────────────────────────────────────────────────────────

def gh_auth_probe() -> AuthProbeResult:
    """`gh auth status` — exit 0 means an authenticated session exists."""
    if shutil.which("gh") is None:
        return AuthProbeResult(ok=False, detail="gh not installed")
    proc = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return AuthProbeResult(ok=True, detail=proc.stdout.strip()[:200])
    return AuthProbeResult(
        ok=False,
        detail=(proc.stderr or proc.stdout or "").strip()[:200] or "gh is not authenticated",
    )


def install_gh_macos() -> bool:
    if shutil.which("brew") is None:
        return False
    return _run(["brew", "install", "gh"])


def install_gh_windows() -> bool:
    if shutil.which("winget") is None:
        return False
    return _run(["winget", "install", "--id", "GitHub.cli", "-e", "--silent"])


def gh_check() -> ToolCheck:
    system = platform.system()
    auto = None
    if system == "Darwin":
        auto = install_gh_macos
    elif system == "Windows":
        auto = install_gh_windows
    return ToolCheck(
        name="gh",
        purpose="GitHub sync (per-project repo create + push)",
        required=True,
        auth_probe=gh_auth_probe,
        auto_install=auto,
        manual_install_hint={
            "Darwin": "brew install gh   # or: https://cli.github.com/",
            "Linux": (
                "Pick the matching package manager for your distro:\n"
                "  Debian/Ubuntu : https://github.com/cli/cli/blob/trunk/docs/install_linux.md#installing-gh-on-linux-and-bsd\n"
                "  Fedora/RHEL    : sudo dnf install gh\n"
                "  Arch           : sudo pacman -S github-cli"
            ),
            "Windows": "winget install --id GitHub.cli   # or scoop install gh",
            "_default": "https://cli.github.com/",
        },
    )


def claude_check() -> ToolCheck:
    return ToolCheck(
        name="claude",
        purpose="Claude Code CLI (subprocess + sdk runners)",
        required=True,
        manual_install_hint={"_default": "Download Claude Code: https://claude.ai/download"},
    )


def codex_check() -> ToolCheck:
    return ToolCheck(
        name="codex",
        purpose="OpenAI Codex CLI (codex runner)",
        required=True,
        manual_install_hint={
            "_default": "npm install -g @openai/codex   # requires Node.js / npm"
        },
    )


def runner_checks_for(runner_names: set[str]) -> list[ToolCheck]:
    """Return the coding-agent CLI checks needed for the configured profiles."""
    checks: list[ToolCheck] = []
    if {"subprocess", "sdk"} & runner_names:
        checks.append(claude_check())
    if "codex" in runner_names:
        checks.append(codex_check())
    return checks


# ── Runner ───────────────────────────────────────────────────────────────────

def run_preflight(
    checks: list[ToolCheck],
    *,
    auto_install: bool = False,
) -> list[CheckResult]:
    """Probe each tool, attempt auto-install when allowed, return results.

    Never raises. Caller is responsible for surfacing failures to the user.
    """
    results: list[CheckResult] = []
    for check in checks:
        results.append(_run_one(check, auto_install=auto_install))
    return results


def _run_one(check: ToolCheck, *, auto_install: bool) -> CheckResult:
    binary_present = shutil.which(check.name) is not None
    installed_now = False

    if not binary_present and auto_install and check.auto_install is not None:
        installed_now = check.auto_install()
        binary_present = shutil.which(check.name) is not None

    auth_ok = True
    auth_detail = ""
    if binary_present and check.auth_probe is not None:
        probe = check.auth_probe()
        auth_ok = probe.ok
        auth_detail = probe.detail

    error = None
    if not binary_present:
        error = f"`{check.name}` not on PATH"
    elif not auth_ok:
        error = f"`{check.name}` is installed but not authenticated"

    return CheckResult(
        tool=check,
        binary_present=binary_present,
        auth_ok=auth_ok,
        auth_detail=auth_detail,
        installed_now=installed_now,
        error=error,
    )


def manual_hint_for(check: ToolCheck) -> str:
    system = platform.system()
    return check.manual_install_hint.get(
        system, check.manual_install_hint.get("_default", "")
    )


def _run(args: list[str]) -> bool:
    proc = subprocess.run(args, capture_output=True, text=True)
    return proc.returncode == 0
