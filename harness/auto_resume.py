"""Cross-platform one-shot `harness resume` scheduling.

Used when a subscription runner reports a usage cap. The orchestrator catches
the rate-limit error, asks this module to schedule a resume shortly after the
reset time, and exits cleanly.

Backends:
- macOS: launchd user LaunchAgent
- Linux: systemd user service + timer
- Windows: Task Scheduler via schtasks + PowerShell wrapper
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape


def backend() -> str | None:
    system = platform.system()
    if system == "Darwin" and shutil.which("launchctl") is not None:
        return "launchd"
    if system == "Linux" and shutil.which("systemctl") is not None:
        return "systemd"
    if system == "Windows" and shutil.which("schtasks") is not None and _powershell() is not None:
        return "task_scheduler"
    return None


def is_supported() -> bool:
    return backend() is not None


def _label(project_id: str) -> str:
    return f"com.harness.resume.{project_id}"


def _task_name(project_id: str) -> str:
    return f"HarnessResume-{project_id}"


def _plist_path(project_id: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(project_id)}.plist"


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_service_path(project_id: str) -> Path:
    return _systemd_dir() / f"{_label(project_id)}.service"


def _systemd_timer_path(project_id: str) -> Path:
    return _systemd_dir() / f"{_label(project_id)}.timer"


def _wrapper_path(project_dir: Path) -> Path:
    return project_dir / ".auto_resume.sh"


def _windows_wrapper_path(project_dir: Path) -> Path:
    return project_dir / ".auto_resume.ps1"


def _log_path(project_dir: Path) -> Path:
    return project_dir / "auto_resume.log"


def cancel(project_id: str) -> None:
    """Best-effort: unload/disable/delete any prior schedule for this project."""
    selected = backend()
    if selected == "launchd":
        _cancel_launchd(project_id)
    elif selected == "systemd":
        _cancel_systemd(project_id)
    elif selected == "task_scheduler":
        _cancel_windows(project_id)


def schedule(
    project_dir: Path,
    project_id: str,
    fire_at_utc: datetime,
    harness_binary: Optional[str] = None,
    buffer_seconds: int = 300,
) -> dict:
    """Schedule one `harness resume` invocation slightly after fire_at_utc."""
    selected = backend()
    if selected is None:
        raise RuntimeError(
            "Auto-resume is not supported on this platform. "
            "Supported backends: macOS launchd, Linux systemd --user, Windows Task Scheduler."
        )

    fire_utc = fire_at_utc + timedelta(seconds=buffer_seconds)
    fire_local = fire_utc.astimezone()
    project_dir = project_dir.resolve()
    harness_binary = harness_binary or shutil.which("harness") or "harness"

    if selected == "launchd":
        return _schedule_launchd(project_dir, project_id, fire_local, fire_utc, harness_binary)
    if selected == "systemd":
        return _schedule_systemd(project_dir, project_id, fire_local, fire_utc, harness_binary)
    return _schedule_windows(project_dir, project_id, fire_local, fire_utc, harness_binary)


# ── macOS launchd ────────────────────────────────────────────────────────────

def _cancel_launchd(project_id: str) -> None:
    plist = _plist_path(project_id)
    if not plist.exists():
        return
    label = _label(project_id)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    _unlink(plist)


def _schedule_launchd(
    project_dir: Path,
    project_id: str,
    fire_local: datetime,
    fire_utc: datetime,
    harness_binary: str,
) -> dict:
    plist = _plist_path(project_id)
    wrapper = _wrapper_path(project_dir)
    log = _log_path(project_dir)
    label = _label(project_id)

    _cancel_launchd(project_id)
    plist.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)
    _write_executable(wrapper, _launchd_wrapper_script(project_dir, harness_binary, plist, label, log))
    plist.write_text(_plist_xml(label, wrapper, log, fire_local))

    uid = os.getuid()
    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True,
        text=True,
    )
    if bootstrap.returncode != 0:
        load = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        if load.returncode != 0:
            raise RuntimeError(
                f"launchctl could not load {plist}: {load.stderr or bootstrap.stderr}"
            )

    return {
        "backend": "launchd",
        "label": label,
        "plist": plist,
        "wrapper": wrapper,
        "log": log,
        "fire_local": fire_local,
        "fire_utc": fire_utc,
        "cancel": f"launchctl bootout gui/$(id -u)/{label}",
    }


def _launchd_wrapper_script(
    project_dir: Path,
    harness_binary: str,
    plist_path: Path,
    label: str,
    log_path: Path,
) -> str:
    return f"""#!/bin/sh
# Auto-generated by harness. Fires once when the usage limit is expected to reset.
set -u

PROJECT_DIR="{project_dir}"
HARNESS="{harness_binary}"
PLIST="{plist_path}"
LABEL="{label}"
LOG="{log_path}"
UID_NUM="$(id -u)"

echo "" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] auto-resume firing" >> "$LOG"

launchctl bootout "gui/${{UID_NUM}}/${{LABEL}}" 2>/dev/null
launchctl unload "$PLIST" 2>/dev/null

cd "$PROJECT_DIR"
"$HARNESS" resume "$PROJECT_DIR" >> "$LOG" 2>&1
EXIT=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] auto-resume exited $EXIT" >> "$LOG"
exit $EXIT
"""


def _plist_xml(label: str, wrapper_path: Path, log_path: Path, fire_local: datetime) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{xml_escape(label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>{xml_escape(str(wrapper_path))}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Month</key>
        <integer>{fire_local.month}</integer>
        <key>Day</key>
        <integer>{fire_local.day}</integer>
        <key>Hour</key>
        <integer>{fire_local.hour}</integer>
        <key>Minute</key>
        <integer>{fire_local.minute}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{xml_escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{xml_escape(str(log_path))}</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


# ── Linux systemd user timer ─────────────────────────────────────────────────

def _cancel_systemd(project_id: str) -> None:
    timer = _systemd_timer_path(project_id)
    service = _systemd_service_path(project_id)
    subprocess.run(["systemctl", "--user", "disable", "--now", timer.name], capture_output=True)
    _unlink(timer)
    _unlink(service)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def _schedule_systemd(
    project_dir: Path,
    project_id: str,
    fire_local: datetime,
    fire_utc: datetime,
    harness_binary: str,
) -> dict:
    service = _systemd_service_path(project_id)
    timer = _systemd_timer_path(project_id)
    wrapper = _wrapper_path(project_dir)
    log = _log_path(project_dir)
    label = _label(project_id)

    _cancel_systemd(project_id)
    service.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)
    _write_executable(wrapper, _systemd_wrapper_script(project_dir, harness_binary, log))
    service.write_text(_systemd_service(label, wrapper))
    timer.write_text(_systemd_timer(label, service.name, fire_local))

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", timer.name], capture_output=True, check=True)

    return {
        "backend": "systemd",
        "label": label,
        "service": service,
        "timer": timer,
        "wrapper": wrapper,
        "log": log,
        "fire_local": fire_local,
        "fire_utc": fire_utc,
        "cancel": f"systemctl --user disable --now {timer.name}",
    }


def _systemd_wrapper_script(project_dir: Path, harness_binary: str, log_path: Path) -> str:
    return f"""#!/bin/sh
# Auto-generated by harness. Fires once when the usage limit is expected to reset.
set -u

PROJECT_DIR="{project_dir}"
HARNESS="{harness_binary}"
LOG="{log_path}"

echo "" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] auto-resume firing" >> "$LOG"
cd "$PROJECT_DIR"
"$HARNESS" resume "$PROJECT_DIR" >> "$LOG" 2>&1
EXIT=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] auto-resume exited $EXIT" >> "$LOG"
exit $EXIT
"""


def _systemd_service(label: str, wrapper: Path) -> str:
    return f"""[Unit]
Description=Harness auto-resume {label}

[Service]
Type=oneshot
ExecStart={wrapper}
"""


def _systemd_timer(label: str, service_name: str, fire_local: datetime) -> str:
    return f"""[Unit]
Description=Harness auto-resume timer {label}

[Timer]
OnCalendar={fire_local.strftime('%Y-%m-%d %H:%M:%S')}
Persistent=false
Unit={service_name}

[Install]
WantedBy=timers.target
"""


# ── Windows Task Scheduler ──────────────────────────────────────────────────

def _cancel_windows(project_id: str) -> None:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _task_name(project_id), "/F"],
        capture_output=True,
    )


def _schedule_windows(
    project_dir: Path,
    project_id: str,
    fire_local: datetime,
    fire_utc: datetime,
    harness_binary: str,
) -> dict:
    task = _task_name(project_id)
    wrapper = _windows_wrapper_path(project_dir)
    log = _log_path(project_dir)
    shell = _powershell() or "powershell"

    _cancel_windows(project_id)
    log.touch(exist_ok=True)
    wrapper.write_text(_powershell_wrapper(project_dir, harness_binary, log, task))

    task_run = f'{shell} -NoProfile -ExecutionPolicy Bypass -File "{wrapper}"'
    subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            task,
            "/TR",
            task_run,
            "/SC",
            "ONCE",
            "/ST",
            fire_local.strftime("%H:%M"),
            "/SD",
            fire_local.strftime("%m/%d/%Y"),
            "/F",
        ],
        capture_output=True,
        check=True,
    )

    return {
        "backend": "task_scheduler",
        "label": task,
        "task": task,
        "wrapper": wrapper,
        "log": log,
        "fire_local": fire_local,
        "fire_utc": fire_utc,
        "cancel": f"schtasks /Delete /TN {task} /F",
    }


def _powershell_wrapper(project_dir: Path, harness_binary: str, log_path: Path, task: str) -> str:
    return f"""# Auto-generated by harness. Fires once when the usage limit is expected to reset.
$ProjectDir = '{_ps_quote(str(project_dir))}'
$Harness = '{_ps_quote(harness_binary)}'
$Log = '{_ps_quote(str(log_path))}'
$Task = '{_ps_quote(task)}'

Add-Content -Path $Log -Value ""
Add-Content -Path $Log -Value "[$(Get-Date -Format o)] auto-resume firing"
schtasks /Delete /TN $Task /F *> $null
Set-Location $ProjectDir
& $Harness resume $ProjectDir *>> $Log
$ExitCode = $LASTEXITCODE
Add-Content -Path $Log -Value "[$(Get-Date -Format o)] auto-resume exited $ExitCode"
exit $ExitCode
"""


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


# ── shared helpers ───────────────────────────────────────────────────────────

def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
