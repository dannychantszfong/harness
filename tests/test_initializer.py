"""Tests for InitializerAgent.

The runner path was previously untested — that's how the
'features.json was a bare list' and 'no <features> block' bugs slipped through.
These tests pin down all five branches:

  • API path                 — _decompose_via_api
  • Runner path: files       — runner wrote features.json + init.sh on disk
  • Runner path: tags        — fallback parsing of <features>/<init_sh>
  • Runner path: nothing     — must raise with last 500 chars of output
  • Idempotent re-run        — features.json exists in either canonical or bare-list shape
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.agents.initializer import InitializerAgent
from harness.config import HarnessConfig
from harness.progress.models import FeatureStatus
from harness.progress.tracker import ProgressTracker


@pytest.fixture
def api_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="testproj",
        brief="A test brief",
        output_dir=str(tmp_path),
        orchestration_mode="api",
    )


@pytest.fixture
def runner_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="testproj",
        brief="A test brief",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
    )


def _stub_runner(output: str = "", side_effect=None) -> MagicMock:
    """Return a CodeRunner-like mock whose .implement() returns a successful RunResult."""
    runner = MagicMock()
    if side_effect is not None:
        runner.implement.side_effect = side_effect
    else:
        result = MagicMock()
        result.success = True
        result.output = output
        result.error = None
        result.input_tokens = None
        result.output_tokens = None
        runner.implement.return_value = result
    return runner


# ── API path ─────────────────────────────────────────────────────────────────

def test_initializer_api_path_uses_tool_use(api_config, monkeypatch):
    """The API path goes through _call() with the set_feature_list tool."""
    agent = InitializerAgent(api_config)
    fake_features = [
        {"id": "f1", "name": "Foo", "description": "Does foo", "priority": 0},
        {"id": "f2", "name": "Bar", "description": "Does bar", "priority": 1},
    ]
    fake_init = "#!/bin/bash\necho hi\n"

    def fake_call(self, system, messages, tools, **kw):
        return "", [{"id": "tu1", "name": "set_feature_list",
                     "input": {"features": fake_features, "init_sh": fake_init}}]

    monkeypatch.setattr(InitializerAgent, "_call", fake_call)
    monkeypatch.setattr(InitializerAgent, "_initial_git_commit", lambda *a, **k: None)

    progress = agent.run(brief=api_config.brief)

    assert len(progress.features) == 2
    assert progress.features[0].id == "f1"
    assert progress.features[0].status == FeatureStatus.PENDING
    assert api_config.init_script_path.exists()
    assert api_config.init_script_path.read_text() == fake_init


# ── Runner path: files-on-disk (preferred, agentic) ──────────────────────────

def test_initializer_runner_reads_files_from_disk(runner_config, monkeypatch):
    """When the runner wrote features.json + init.sh directly, prefer those."""
    feat_data = [
        {"id": "f1", "name": "X", "description": "x", "priority": 0},
        {"id": "f2", "name": "Y", "description": "y", "priority": 1},
    ]

    def fake_runner_call(prompt: str) -> str:
        # Simulate Claude Code writing files as a side effect of the prompt
        runner_config.features_path.write_text(json.dumps(feat_data))
        runner_config.init_script_path.write_text("#!/bin/bash\necho ready\n")
        return "INIT_DONE"

    agent = InitializerAgent(runner_config, runner=_stub_runner())
    monkeypatch.setattr(InitializerAgent, "_call_via_runner", lambda self, p: fake_runner_call(p))
    monkeypatch.setattr(InitializerAgent, "_initial_git_commit", lambda *a, **k: None)

    # First time through: features.json doesn't exist yet, runner writes it
    # mid-call. Note this is exactly the live-system behavior we observed.
    progress = agent.run(brief=runner_config.brief)

    assert [f.id for f in progress.features] == ["f1", "f2"]
    assert progress.features[0].status == FeatureStatus.PENDING


def test_initializer_runner_handles_dict_envelope(runner_config, monkeypatch):
    """Some runners write {'features': [...]} instead of a bare list."""
    enveloped = {"features": [{"id": "f1", "name": "Z", "description": "z", "priority": 0}]}

    def fake_runner_call(prompt: str) -> str:
        runner_config.features_path.write_text(json.dumps(enveloped))
        runner_config.init_script_path.write_text("#!/bin/bash\n")
        return "done"

    agent = InitializerAgent(runner_config, runner=_stub_runner())
    monkeypatch.setattr(InitializerAgent, "_call_via_runner", lambda self, p: fake_runner_call(p))
    monkeypatch.setattr(InitializerAgent, "_initial_git_commit", lambda *a, **k: None)

    progress = agent.run(brief=runner_config.brief)
    assert len(progress.features) == 1
    assert progress.features[0].id == "f1"


# ── Runner path: tag-based fallback ──────────────────────────────────────────

def test_initializer_runner_tag_fallback(runner_config, monkeypatch):
    """If the runner returned text instead of writing files, parse the tags."""
    output = (
        "Here's the plan:\n\n"
        '<features>\n[{"id":"f1","name":"A","description":"a","priority":0}]\n</features>\n\n'
        "<init_sh>\n#!/bin/bash\necho started\n</init_sh>\n"
    )
    agent = InitializerAgent(runner_config, runner=_stub_runner())
    monkeypatch.setattr(InitializerAgent, "_call_via_runner", lambda self, p: output)
    monkeypatch.setattr(InitializerAgent, "_initial_git_commit", lambda *a, **k: None)

    progress = agent.run(brief=runner_config.brief)
    assert len(progress.features) == 1
    assert progress.features[0].id == "f1"
    assert "echo started" in runner_config.init_script_path.read_text()


def test_initializer_runner_no_features_raises_with_context(runner_config, monkeypatch):
    """The exact error the user saw — runner produced nothing parseable."""
    output = "I'm thinking... I'll get started in a moment."
    agent = InitializerAgent(runner_config, runner=_stub_runner())
    monkeypatch.setattr(InitializerAgent, "_call_via_runner", lambda self, p: output)
    monkeypatch.setattr(InitializerAgent, "_initial_git_commit", lambda *a, **k: None)

    with pytest.raises(RuntimeError) as exc:
        agent.run(brief=runner_config.brief)
    msg = str(exc.value)
    assert "no features.json on disk" in msg or "no\nfeatures.json" in msg
    # Output context is included for debugging, not silently swallowed
    assert "thinking" in msg


# ── Promote bare-list features.json (resume after agentic side-effect) ───────

def test_initializer_promotes_bare_list_features(runner_config):
    """An existing bare-list features.json is normalized to ProjectProgress shape."""
    bare = [
        {"id": "f1", "name": "A", "description": "a", "priority": 0},
        {"id": "f2", "name": "B", "description": "b", "priority": 1},
        {"id": "f3", "name": "C", "description": "c", "priority": 2},
    ]
    runner_config.features_path.write_text(json.dumps(bare))

    # No runner needed — promotion is a pure file-shape upgrade
    agent = InitializerAgent(runner_config)
    progress = agent.run(brief=runner_config.brief)

    assert len(progress.features) == 3
    # Re-load proves it was rewritten in canonical shape
    reloaded = ProgressTracker(runner_config).load()
    assert reloaded.project_name == "testproj"
    assert reloaded.brief == runner_config.brief
    assert [f.id for f in reloaded.features] == ["f1", "f2", "f3"]
    assert all(f.status == FeatureStatus.PENDING for f in reloaded.features)


def test_initializer_skips_when_canonical_features_exist(runner_config):
    """Existing canonical ProjectProgress is loaded as-is, no re-decomposition."""
    tracker = ProgressTracker(runner_config)
    from harness.progress.models import Feature, ProjectProgress
    progress = ProjectProgress(
        project_name="testproj",
        brief=runner_config.brief,
        features=[Feature(id="x1", name="X", description="x", priority=0)],
    )
    tracker.save(progress)

    # Inject a runner that would crash if called — proves we skipped re-decomp
    bomb = MagicMock()
    bomb.implement.side_effect = AssertionError("should not be called")
    agent = InitializerAgent(runner_config, runner=bomb)
    loaded = agent.run(brief=runner_config.brief)
    assert loaded.features[0].id == "x1"
    bomb.implement.assert_not_called()
