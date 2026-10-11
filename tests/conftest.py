"""Shared fixtures.

The suite must pass on a machine with no `claude`, `codex` or `gh` on PATH
(CI installs none of them), so nothing here may depend on real binaries.
"""

import pytest

from harness.runners.base import PreflightResult
from harness.runners.codex_runner import CodexRunner
from harness.runners.sdk_runner import SDKRunner
from harness.runners.subprocess_runner import SubprocessRunner


@pytest.fixture
def runners_ready(monkeypatch):
    """Make every runner's preflight pass without the coding-agent CLIs.

    Orchestrator.__init__ runs real preflight; tests that build an
    Orchestrator around real runner classes use this fixture.
    """

    def _ok(self):
        return PreflightResult(ok=True, summary=f"stub {self.runner_type.value}", details="test stub")

    for cls in (SubprocessRunner, SDKRunner, CodexRunner):
        monkeypatch.setattr(cls, "preflight", _ok)
