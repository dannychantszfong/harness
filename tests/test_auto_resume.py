"""Tests for rate-limit detection + cross-platform auto-resume scheduling.

The launchctl end-to-end tests run only on macOS. Linux/systemd and
Windows/Task Scheduler paths are unit-tested with subprocess mocked.
"""

import os
import platform
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness import auto_resume
from harness.config import CONFIG_FILENAME, HarnessConfig
from harness.orchestrator import Orchestrator
from harness.runners.base import (
    PreflightResult, RunnerRateLimitedError, RunResult, RunnerType,
)
from harness.runners.subprocess_runner import _parse_reset_time


_DARWIN = platform.system() == "Darwin"


# ── Parser ───────────────────────────────────────────────────────────────────

class TestParseResetTime:
    """The parser must only fire when the trigger phrase is present, and
    must always return a tz-aware UTC datetime in the future."""

    NOON_UTC = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

    def test_parses_pm_london(self):
        # London is BST (+01:00) on May 5, so 9:30pm London = 20:30 UTC
        out = _parse_reset_time(
            "You've hit your limit · resets 9:30pm (Europe/London)",
            now_utc=self.NOON_UTC,
        )
        assert out is not None
        assert out.tzinfo is not None
        assert out.hour == 20 and out.minute == 30

    def test_parses_am_with_implicit_zero_minutes(self):
        out = _parse_reset_time(
            "you've hit your limit · resets 12am (UTC)",
            now_utc=self.NOON_UTC,
        )
        assert out is not None
        # 12am next day in UTC since 12am today is past noon.
        assert out.hour == 0 and out.minute == 0

    def test_case_insensitive(self):
        out = _parse_reset_time(
            "YOU'VE HIT YOUR LIMIT · resets 11PM (America/New_York)",
            now_utc=self.NOON_UTC,
        )
        assert out is not None

    def test_returns_none_without_trigger(self):
        # The phrase "resets 9pm" alone must not be enough — only fire when
        # the runner explicitly reports a usage cap.
        assert _parse_reset_time("resets 9pm (UTC)", now_utc=self.NOON_UTC) is None

    def test_returns_none_for_unparseable_zone(self):
        assert _parse_reset_time(
            "you've hit your limit · resets 9pm (Made/Up)",
            now_utc=self.NOON_UTC,
        ) is None

    def test_rolls_to_tomorrow_if_time_already_past(self):
        # At 23:00 UTC, "9:30pm Europe/London" (= 20:30 UTC) is already past.
        late = datetime(2026, 5, 5, 23, 0, tzinfo=timezone.utc)
        out = _parse_reset_time(
            "you've hit your limit · resets 9:30pm (Europe/London)",
            now_utc=late,
        )
        assert out is not None
        # Must be the next 20:30 UTC, not today's
        assert out > late


# ── Module-level helpers ─────────────────────────────────────────────────────

def test_label_is_deterministic_per_project():
    assert auto_resume._label("abc12345") == "com.harness.resume.abc12345"
    assert auto_resume._label("abc12345") != auto_resume._label("xyz98765")


def test_is_supported_matches_platform():
    assert auto_resume.is_supported() is (auto_resume.backend() is not None)


def test_backend_detection_for_each_platform(monkeypatch):
    def fake_which_factory(names):
        return lambda name: f"/bin/{name}" if name in names else None

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(auto_resume.shutil, "which", fake_which_factory({"launchctl"}))
    assert auto_resume.backend() == "launchd"

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(auto_resume.shutil, "which", fake_which_factory({"systemctl"}))
    assert auto_resume.backend() == "systemd"

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(auto_resume.shutil, "which", fake_which_factory({"schtasks", "powershell"}))
    assert auto_resume.backend() == "task_scheduler"


def test_linux_systemd_schedule_writes_units(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    systemd_dir = tmp_path / "systemd"
    calls = []

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(auto_resume.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(auto_resume, "_systemd_dir", lambda: systemd_dir)

    def fake_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    scheduled = auto_resume.schedule(
        project_dir=project_dir,
        project_id="linux01",
        fire_at_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        harness_binary="/usr/local/bin/harness",
        buffer_seconds=0,
    )

    assert scheduled["backend"] == "systemd"
    assert scheduled["service"].exists()
    assert scheduled["timer"].exists()
    assert "OnCalendar=" in scheduled["timer"].read_text()
    assert ["systemctl", "--user", "enable", "--now", scheduled["timer"].name] in calls


def test_windows_task_scheduler_schedule_writes_wrapper(tmp_path, monkeypatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    calls = []

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        auto_resume.shutil,
        "which",
        lambda name: {
            "schtasks": "C:/Windows/System32/schtasks.exe",
            "powershell": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "harness": "C:/Tools/harness.exe",
        }.get(name),
    )

    def fake_run(args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    scheduled = auto_resume.schedule(
        project_dir=project_dir,
        project_id="win01",
        fire_at_utc=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        buffer_seconds=0,
    )

    assert scheduled["backend"] == "task_scheduler"
    assert scheduled["wrapper"].suffix == ".ps1"
    assert "harness.exe" in scheduled["wrapper"].read_text()
    create_call = next(args for args in calls if args[:2] == ["schtasks", "/Create"])
    assert "/SC" in create_call
    assert "ONCE" in create_call


# ── End-to-end (Darwin only) ─────────────────────────────────────────────────

@pytest.mark.skipif(not _DARWIN, reason="launchd is macOS-only")
def test_orchestrator_catches_rate_limit_and_schedules(tmp_path, monkeypatch):
    """The full path: runner returns a rate-limited RunResult →
    agents/base raises RunnerRateLimitedError → orchestrator catches it,
    prints the panel, and schedules launchd. After the test we cancel,
    so nothing real is left scheduled."""
    project_dir = tmp_path / "rl_proj"
    project_dir.mkdir()
    pid = "rltest01"
    cfg = HarnessConfig(
        project_name="RL", project_id=pid, brief="x",
        output_dir=str(project_dir),
        orchestration_mode="runner", code_runner="subprocess",
        auto_resume_on_rate_limit=True,
    )
    cfg.save_yaml(project_dir / CONFIG_FILENAME)

    fake_runner = MagicMock()
    fake_runner.preflight.return_value = PreflightResult(
        ok=True, summary="stub", details="stub"
    )
    fake_runner.implement.return_value = RunResult(
        output="",
        success=False,
        error="rate-limited",
        rate_limit_reset_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    monkeypatch.setattr(
        "harness.orchestrator.create_runner",
        lambda *a, **k: fake_runner,
    )

    try:
        orch = Orchestrator(cfg, runner_type=RunnerType.SUBPROCESS)
        # Must NOT raise; orchestrator handles the rate-limit cleanly.
        orch.run()

        plist = auto_resume._plist_path(pid)
        wrapper = auto_resume._wrapper_path(project_dir.resolve())
        assert plist.exists(), "expected plist to be written"
        assert wrapper.exists(), "expected wrapper script to be written"
        assert os.access(str(wrapper), os.X_OK), "wrapper not executable"
    finally:
        # Always clean up so we don't leave a real launchd job scheduled
        auto_resume.cancel(pid)
        assert not auto_resume._plist_path(pid).exists()


@pytest.mark.skipif(not _DARWIN, reason="launchd is macOS-only")
def test_auto_resume_disabled_skips_scheduling(tmp_path, monkeypatch):
    project_dir = tmp_path / "rl_off"
    project_dir.mkdir()
    pid = "rltest02"
    cfg = HarnessConfig(
        project_name="RL", project_id=pid, brief="x",
        output_dir=str(project_dir),
        orchestration_mode="runner", code_runner="subprocess",
        auto_resume_on_rate_limit=False,
    )
    cfg.save_yaml(project_dir / CONFIG_FILENAME)

    fake_runner = MagicMock()
    fake_runner.preflight.return_value = PreflightResult(
        ok=True, summary="stub", details="stub"
    )
    fake_runner.implement.return_value = RunResult(
        output="", success=False, error="rate-limited",
        rate_limit_reset_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    monkeypatch.setattr(
        "harness.orchestrator.create_runner", lambda *a, **k: fake_runner,
    )

    orch = Orchestrator(cfg, runner_type=RunnerType.SUBPROCESS)
    orch.run()  # must not raise
    # Nothing should be scheduled when the flag is off
    assert not auto_resume._plist_path(pid).exists()
