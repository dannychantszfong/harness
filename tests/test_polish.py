"""Phase E polish: P-18, P-21, P-25, P-26, P-28 and C-07 (experimental)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.config import HarnessConfig, RunnerProfile
from harness.progress.models import Feature, ProjectProgress
from harness.runner_profiles import HarnessSetup, apply_setup_defaults
from harness.runners.base import PreflightResult, RunResult, RunnerType

ROOT = Path(__file__).resolve().parents[1]


# ── P-18: runner mode is the default everywhere ─────────────────────────────

def test_default_orchestration_mode_is_runner():
    assert HarnessConfig(project_name="t", brief="b").orchestration_mode == "runner"


@pytest.mark.parametrize("example", ["web_app.yaml", "game.yaml"])
def test_examples_load_in_runner_mode(example):
    cfg = HarnessConfig.from_file(ROOT / "examples" / example)
    assert cfg.orchestration_mode == "runner"
    assert cfg.code_runner == "subprocess"


# ── P-21: setup never silently flips an explicit api-mode project ───────────

def _setup():
    return HarnessSetup(
        runner_profiles=[RunnerProfile(name="claude", runner="subprocess", model="sonnet")],
        generator_runner_order=["claude"],
    )


def test_setup_keeps_explicit_api_mode(tmp_path):
    path = tmp_path / "harness_config.json"
    HarnessConfig(project_name="t", brief="b", orchestration_mode="api").save_file(path)
    cfg = HarnessConfig.from_file(path)

    changed = apply_setup_defaults(cfg, _setup())

    assert cfg.orchestration_mode == "api"
    assert "orchestration_mode" not in changed
    assert "runner_profiles" in changed and "code_runner" in changed


def test_setup_fills_mode_only_when_unset():
    cfg = HarnessConfig.model_validate({"project_name": "t", "brief": "b", "orchestration_mode": "api"})
    cfg_unset = HarnessConfig.model_validate({"project_name": "t", "brief": "b"})
    object.__setattr__(cfg_unset, "orchestration_mode", "api")  # value without being "set"
    cfg_unset.model_fields_set.discard("orchestration_mode")
    apply_setup_defaults(cfg, _setup())
    apply_setup_defaults(cfg_unset, _setup())
    assert cfg.orchestration_mode == "api"
    assert cfg_unset.orchestration_mode == "runner"


def test_apply_setup_defaults_reports_nothing_when_no_setup():
    assert apply_setup_defaults(HarnessConfig(project_name="t", brief="b"), HarnessSetup()) == []


# ── P-25: preflight runs even with runner profiles ──────────────────────────

def _fake_runner_factory(created, failing=()):
    def factory(runner_type, cfg):
        runner = MagicMock()
        runner.runner_type = runner_type
        ok = runner_type.value not in failing
        runner.preflight.return_value = PreflightResult(
            ok=ok, summary=f"{runner_type.value} summary", details="details",
            error=None if ok else f"`{runner_type.value}` binary not found on PATH.",
        )
        runner.implement.return_value = RunResult(output="", success=True)
        created.append(runner)
        return runner
    return factory


def _profiles_config(tmp_path):
    return HarnessConfig(
        project_name="t", brief="b", output_dir=str(tmp_path), code_runner="subprocess",
        runner_profiles=[
            RunnerProfile(name="claude", runner="subprocess"),
            RunnerProfile(name="codex", runner="codex"),
        ],
        planner_runner_order=["claude"],
        generator_runner_order=["claude", "codex"],
        evaluator_runner_order=["codex"],
    )


def test_profiles_preflight_every_runtime_in_use(tmp_path, monkeypatch, capsys):
    from harness.orchestrator import Orchestrator

    created = []
    factory = _fake_runner_factory(created)
    monkeypatch.setattr("harness.orchestrator.create_runner", factory)
    monkeypatch.setattr("harness.runner_profiles.create_runner", factory)

    Orchestrator(_profiles_config(tmp_path))

    checked = sorted(r.runner_type.value for r in created if r.preflight.called)
    assert checked == ["codex", "subprocess"]  # one per runtime, deduplicated
    out = capsys.readouterr().out
    assert "Runner rotation enabled" in out and "Runner ready" in out


def test_profiles_preflight_failure_aborts(tmp_path, monkeypatch, capsys):
    from harness.orchestrator import Orchestrator

    created = []
    factory = _fake_runner_factory(created, failing={"codex"})
    monkeypatch.setattr("harness.orchestrator.create_runner", factory)
    monkeypatch.setattr("harness.runner_profiles.create_runner", factory)

    with pytest.raises(SystemExit):
        Orchestrator(_profiles_config(tmp_path))
    out = capsys.readouterr().out
    assert "codex" in out and "evaluator" in out and "Cannot start" in out


# ── P-26: the checklist names the current feature ───────────────────────────

def test_session_checklist_points_at_the_task(tmp_path):
    from harness.session.opener import SessionOpener

    cfg = HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path))
    text = SessionOpener(cfg).build_opening_context(
        ProjectProgress(project_name="t", brief="b", features=[Feature(id="f1", name="A", description="a")]),
        handoff=None,
        include_git_log=False,
    )
    assert "Work only on the feature named in *Your task*" in text
    assert "highest-priority" not in text


# ── P-28: api-mode text blocks are concatenated ─────────────────────────────

def test_api_call_concatenates_text_blocks(tmp_path, monkeypatch):
    from harness.agents.reviewer import ReviewerAgent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="api")
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="part one "), SimpleNamespace(type="text", text="part two")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
    )
    text, _ = ReviewerAgent(cfg, client=client)._call(system="s", messages=[], stream=False)
    assert text == "part one part two"
    assert client.messages.create.call_args.kwargs["max_tokens"] == 8192


# ── C-07 (experimental): Playwright MCP for the evaluator ───────────────────

@pytest.fixture
def browser_cfg(tmp_path) -> HarnessConfig:
    return HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="runner",
                         evaluator_browser_verification=True)


def _evaluation_output():
    return ("<evaluation><design_quality>8</design_quality><originality>8</originality><craft>8</craft>"
            "<functionality>8</functionality><feedback>ok</feedback></evaluation>")


def test_browser_evaluator_gets_mcp_config_and_tools(browser_cfg):
    from harness.agents.evaluator import EvaluatorAgent

    runner = MagicMock()
    runner.runner_type = RunnerType.SUBPROCESS
    runner.implement.return_value = RunResult(output=_evaluation_output(), success=True)
    EvaluatorAgent(browser_cfg, runner=runner).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)

    access = runner.implement.call_args.kwargs["access"]
    assert access.read_only and "mcp__playwright" in access.allowed_tools
    mcp = json.loads(Path(access.mcp_config).read_text())
    assert mcp["mcpServers"]["playwright"]["command"] == "npx"
    assert "Playwright browser tools" in runner.implement.call_args.args[0]


def test_subprocess_passes_mcp_config(browser_cfg):
    from harness.runners.base import RunnerAccess
    from harness.runners.subprocess_runner import SubprocessRunner

    cmd = SubprocessRunner(browser_cfg).build_command(RunnerAccess(read_only=True, allowed_tools=("Read",),
                                                                   mcp_config="/x/mcp.json"))
    assert cmd[cmd.index("--mcp-config") + 1] == "/x/mcp.json"


def test_browser_is_skipped_on_codex(browser_cfg):
    from harness.agents.evaluator import EvaluatorAgent

    runner = MagicMock()
    runner.runner_type = RunnerType.CODEX
    runner.implement.return_value = RunResult(output=_evaluation_output(), success=True)
    EvaluatorAgent(browser_cfg, runner=runner).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)
    assert runner.implement.call_args.kwargs["access"].mcp_config is None
    assert "Playwright" not in runner.implement.call_args.args[0]


def test_browser_preflight_requires_launchable_server(browser_cfg, monkeypatch):
    from harness.agents.browser import browser_preflight_error

    monkeypatch.setattr("harness.agents.browser.shutil.which", lambda name: None)
    assert "npx" in browser_preflight_error(browser_cfg)
    monkeypatch.setattr("harness.agents.browser.shutil.which", lambda name: f"/usr/bin/{name}")
    assert browser_preflight_error(browser_cfg) is None


def test_browser_off_by_default(tmp_path):
    from harness.agents.evaluator import EvaluatorAgent

    cfg = HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="runner")
    runner = MagicMock()
    runner.runner_type = RunnerType.SUBPROCESS
    assert EvaluatorAgent(cfg, runner=runner)._runner_access().mcp_config is None
    assert not (tmp_path / ".harness" / "playwright-mcp.json").exists()
