"""Evaluation integrity (Phase C): P-05..P-09, C-03, C-05, C-06."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from harness.agents.evaluator import (
    EvaluatorAgent,
    _parse_runner_scores,
    runner_score_instruction,
    system_prompt,
)
from harness.agents.generator import GeneratorAgent, parse_sprint_contract
from harness.agents.tools import ProjectTools
from harness.config import HarnessConfig
from harness.progress.models import EvaluationResult, Feature, ProjectProgress
from harness.progress.tracker import ProgressTracker
from harness.runners.base import RunnerAccess, RunResult


@pytest.fixture
def cfg(tmp_path) -> HarnessConfig:
    return HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="runner")


def _block(dq="8", orig="8", craft="8", fn="8", overall=None, feedback="Specific feedback.") -> str:
    overall_tag = f"<overall_score>{overall}</overall_score>" if overall is not None else ""
    return (
        "Some analysis first.\n<evaluation>"
        f"<design_quality>{dq}</design_quality><originality>{orig}</originality>"
        f"<craft>{craft}</craft><functionality>{fn}</functionality>{overall_tag}"
        f"<feedback>{feedback}</feedback></evaluation>"
    )


# ── P-05 / P-08 / P-09 / P-07: the runner-mode parser ───────────────────────

@pytest.mark.parametrize("text, expect", [
    # P-05: the model's own overall is ignored — Harness computes 5.0
    (_block("5", "5", "5", "5", overall="9.5"), {"overall": 5.0, "passed": False}),
    (_block("9", "9", "9", "9"), {"overall": 9.0, "passed": True}),
    # P-09 / D-01: 9/9/9/3 = 8.1 ≥ 8.0 but functionality < 6 → fails
    (_block("9", "9", "9", "3"), {"overall": 8.1, "passed": False}),
    (_block("9", "9", "9", "6"), {"overall": 8.55, "passed": True}),
    # P-08: out-of-range values are clamped
    (_block("95", "9", "9", "9"), {"overall": 9.3, "passed": True, "dq": 10.0}),
    (_block("0", "9", "9", "9"), {"dq": 1.0}),
    # P-07 / P-08: malformed numbers or a missing block are parse failures
    (_block("..", "9", "9", "9"), {"parse_failed": True}),
    (_block("8/10", "9", "9", "9"), {"parse_failed": True}),
    (_block("", "9", "9", "9"), {"parse_failed": True}),
    ("I evaluated it carefully and it is great.", {"parse_failed": True}),
    ("<evaluation><design_quality>9</design_quality></evaluation>", {"parse_failed": True}),
])
def test_parse_runner_scores(cfg, text, expect):
    result = _parse_runner_scores(text, 1, cfg)
    if expect.get("parse_failed"):
        assert result.parse_failed is True
        assert result.passed is False
        assert result.overall_score == 0.0  # never a silent 5.0
        assert "not graded" in result.feedback
        return
    assert result.parse_failed is False
    if "overall" in expect:
        assert result.overall_score == pytest.approx(expect["overall"])
    if "passed" in expect:
        assert result.passed is expect["passed"]
    if "dq" in expect:
        assert result.design_quality == expect["dq"]


def test_parser_uses_the_last_block_and_keeps_reported_overall(cfg):
    text = "Template: " + _block("1", "1", "1", "1") + "\nMy answer: " + _block("9", "9", "9", "9", overall="2")
    result = _parse_runner_scores(text, 3, cfg)
    assert result.overall_score == pytest.approx(9.0)
    assert result.reported_overall == 2.0  # diagnostics only
    assert result.iteration == 3


def test_configured_weights_drive_the_score(tmp_path):
    cfg = HarnessConfig(
        project_name="t", brief="b", output_dir=str(tmp_path),
        evaluator_weights={"design_quality": 0.1, "originality": 0.1, "craft": 0.1, "functionality": 0.7},
    )
    result = _parse_runner_scores(_block("10", "10", "10", "6"), 1, cfg)
    assert result.overall_score == pytest.approx(7.2)


# ── P-06: weights validated on load and rendered into prompts ───────────────

def test_weights_must_sum_to_one(tmp_path):
    with pytest.raises(ValidationError, match="sum to 1.0"):
        HarnessConfig(project_name="t", brief="b",
                      evaluator_weights={"design_quality": 0.5, "originality": 0.5, "craft": 0.5, "functionality": 0.5})


def test_prompts_render_configured_weights_and_floor(tmp_path):
    cfg = HarnessConfig(
        project_name="t", brief="b", output_dir=str(tmp_path),
        evaluator_weights={"design_quality": 0.25, "originality": 0.25, "craft": 0.25, "functionality": 0.25},
        evaluator_functionality_floor=7.0,
    )
    sys_prompt = system_prompt(cfg)
    instruction = runner_score_instruction(cfg)
    assert sys_prompt.count("(weight: 25%)") == 4
    assert "30%" not in sys_prompt and "0.30" not in instruction
    assert "functionality×0.25" in instruction
    assert "least 7" in sys_prompt
    # No Playwright sentence unless browser verification is configured (C-07)
    assert "Playwright" not in sys_prompt


def test_functionality_floor_default():
    assert HarnessConfig(project_name="t", brief="b").evaluator_functionality_floor == 6.0


# ── C-06: role-appropriate permissions in runner mode ───────────────────────

def test_evaluator_session_is_read_only_with_allow_listed_commands(cfg):
    cfg.test_command = "pytest -q"
    runner = MagicMock()
    runner.implement.return_value = RunResult(output=_block(), success=True)
    EvaluatorAgent(cfg, runner=runner).evaluate(Feature(id="f1", name="A", description="a"), "self", 1)

    access = runner.implement.call_args.kwargs["access"]
    assert isinstance(access, RunnerAccess) and access.read_only is True
    tools = access.allowed_tools
    assert "Read" in tools and "Grep" in tools
    assert "Write" not in tools and "Edit" not in tools
    assert f"Bash({cfg.startup_command_for_platform})" in tools
    assert "Bash(pytest -q *)" in tools
    assert "Bash(git diff)" in tools and "Bash(git log --oneline -20)" in tools
    # No wildcard git rules: `git log --output=<file>` would be a write
    assert not any(t.startswith("Bash(git") and t.endswith("*)") for t in tools)


def test_generator_keeps_full_access(cfg):
    runner = MagicMock()
    runner.implement.return_value = RunResult(output="ok", success=True)
    GeneratorAgent(cfg, runner=runner).implement_feature(
        feature=Feature(id="f1", name="A", description="a"), progress=MagicMock(), session_preamble="",
    )
    assert "access" not in runner.implement.call_args.kwargs


def test_role_allowed_tools_override(cfg):
    cfg.role_allowed_tools = {"evaluator": ["Read", "Bash(npm test *)"], "generator": ["Read", "Edit"]}
    ev = EvaluatorAgent(cfg)._runner_access()
    assert ev.allowed_tools == ("Read", "Bash(npm test *)") and ev.read_only
    gen = GeneratorAgent(cfg, runner=MagicMock())._runner_access()
    assert gen.allowed_tools == ("Read", "Edit") and not gen.read_only


def test_subprocess_restricted_command(cfg):
    from harness.runners.subprocess_runner import SubprocessRunner

    access = RunnerAccess(read_only=True, allowed_tools=("Read", "Bash(git diff *)"))
    cmd = SubprocessRunner(cfg).build_command(access)
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    allowed = cmd[cmd.index("--allowedTools") + 1: cmd.index("--disallowedTools")]
    assert allowed == ["Read", "Bash(git diff *)"]
    assert {"Write", "Edit", "MultiEdit"} <= set(cmd[cmd.index("--disallowedTools") + 1:])
    assert SubprocessRunner(cfg).build_command(None)[4] == "--dangerously-skip-permissions"


def test_codex_read_only_sandbox(cfg):
    from harness.runners.codex_runner import CodexRunner

    cmd = CodexRunner(cfg).build_command("/tmp/x", RunnerAccess(read_only=True))
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in CodexRunner(cfg).build_command("/tmp/x")


def test_sdk_restricted_options(cfg):
    from harness.runners.sdk_runner import SDKRunner

    kwargs = SDKRunner(cfg)._options_kwargs("/x", RunnerAccess(read_only=True, allowed_tools=("Read",)))
    assert kwargs["permission_mode"] == "default"
    assert kwargs["allowed_tools"] == ["Read"]
    assert "Write" in kwargs["disallowed_tools"]
    assert SDKRunner(cfg)._options_kwargs("/x")["permission_mode"] == "bypassPermissions"


# ── C-03: sprint contract in runner mode ────────────────────────────────────

_CONTRACT = """Here you go.
<sprint_contract>
  <acceptance_criteria>
    <criterion>Login form accepts email and password</criterion>
    <criterion>Wrong password shows an error</criterion>
  </acceptance_criteria>
  <out_of_scope>
    <item>Password reset</item>
  </out_of_scope>
</sprint_contract>"""


def test_runner_sprint_contract_is_negotiated(cfg):
    runner = MagicMock()
    runner.implement.return_value = RunResult(output=_CONTRACT, success=True)
    feature = Feature(id="f1", name="Login", description="signup + login")

    contract = GeneratorAgent(cfg, runner=runner).negotiate_sprint_contract(feature, spec="spec")

    assert contract.acceptance_criteria == ["Login form accepts email and password", "Wrong password shows an error"]
    assert contract.out_of_scope == ["Password reset"]
    prompt = runner.implement.call_args.args[0]
    assert "proposing the sprint contract" in prompt and "<sprint_contract>" in prompt
    assert runner.implement.call_args.kwargs["access"].read_only is True  # planning, not coding


@pytest.mark.parametrize("output, success", [("no block here", True), ("", False)])
def test_runner_sprint_contract_falls_back(cfg, output, success):
    runner = MagicMock()
    runner.implement.return_value = RunResult(output=output, success=success, error=None if success else "boom")
    feature = Feature(id="f1", name="Login", description="signup + login")
    contract = GeneratorAgent(cfg, runner=runner).negotiate_sprint_contract(feature, spec="")
    assert contract.acceptance_criteria == ["signup + login"]


def test_parse_sprint_contract_requires_criteria():
    assert parse_sprint_contract("<sprint_contract><out_of_scope><item>x</item></out_of_scope></sprint_contract>",
                                 Feature(id="f", name="n", description="d")) is None


# ── P-07: one re-ask before an unparseable answer counts ────────────────────

def _eval(passed=True, parse_failed=False, iteration=1):
    return EvaluationResult(
        design_quality=9, originality=9, craft=9, functionality=9, overall_score=9.0 if passed else 0.0,
        feedback="x", passed=passed, iteration=iteration, parse_failed=parse_failed,
    )


@pytest.mark.usefixtures("runners_ready")
@pytest.mark.parametrize("second, expect_status, expect_history", [
    (_eval(passed=True), "passing", 1),
    (_eval(passed=False, parse_failed=True), "failing", 1),
])
def test_unparseable_evaluation_is_retried_once(tmp_path, second, expect_status, expect_history):
    from harness.orchestrator import Orchestrator

    cfg = HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="runner",
                        max_iterations_per_feature=1, sprint_contract_enabled=False)
    progress = ProjectProgress(project_name="t", brief="b", spec="s",
                               features=[Feature(id="f1", name="A", description="a")])
    ProgressTracker(cfg).save(progress)
    orch = Orchestrator(cfg)
    with (
        patch.object(orch, "_initialize", return_value=progress),
        patch.object(orch, "_plan", return_value=progress),
        patch("harness.orchestrator.GeneratorAgent") as MockGen,
        patch("harness.orchestrator.EvaluatorAgent") as MockEval,
    ):
        MockGen.return_value.implement_feature.return_value = "self eval"
        MockGen.return_value.usage = MagicMock(total_tokens=0, cost_usd=0.0)
        ev = MockEval.return_value
        ev.usage = MagicMock(total_tokens=0, cost_usd=0.0)
        ev.last_raw_output = "I forgot the XML block"
        ev.evaluate.side_effect = [_eval(passed=False, parse_failed=True), second]
        orch.run()

    assert ev.evaluate.call_count == 2
    assert ev.evaluate.call_args_list[1].kwargs["previous_output"] == "I forgot the XML block"
    f1 = ProgressTracker(cfg).load().get_feature("f1")
    assert f1.status.value == expect_status
    assert len(f1.evaluation_history) == expect_history  # the retry does not count twice


def test_retry_prompt_contains_previous_output(cfg):
    runner = MagicMock()
    runner.implement.return_value = RunResult(output=_block(), success=True)
    EvaluatorAgent(cfg, runner=runner).evaluate(
        Feature(id="f1", name="A", description="a"), "self", 1, previous_output="ANALYSIS WITHOUT XML",
    )
    prompt = runner.implement.call_args.args[0]
    assert "ANALYSIS WITHOUT XML" in prompt and "required <evaluation> XML block" in prompt


def test_evaluator_session_failure_is_an_explicit_parse_failure(cfg):
    runner = MagicMock()
    runner.implement.return_value = RunResult(output="", success=False, error="claude crashed")
    result = EvaluatorAgent(cfg, runner=runner).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)
    assert result.parse_failed and result.failure_reason == "evaluator_error"
    assert "claude crashed" in result.feedback


# ── C-05: api-mode evaluator inspects the project ───────────────────────────

def _tool_use(name, inp, id_="t1"):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def _response(*blocks):
    return SimpleNamespace(content=list(blocks), usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                                        cache_read_input_tokens=0,
                                                                        cache_creation_input_tokens=0))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **params):
        self.calls.append(params)
        return self.responses.pop(0)


@pytest.fixture
def api_cfg(tmp_path, monkeypatch) -> HarnessConfig:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    (tmp_path / "app.py").write_text("def login():\n    return 'ok'\n")
    return HarnessConfig(project_name="t", brief="b", output_dir=str(tmp_path), orchestration_mode="api",
                         startup_command=f"{Path(sys.executable).as_posix()} -c \"print('started')\"")


def test_api_evaluator_reads_files_then_submits(api_cfg):
    client = FakeClient([
        _response(_tool_use("read_file", {"path": "app.py"})),
        _response(_tool_use("submit_evaluation", {
            "design_quality": 9, "originality": 9, "craft": 9, "functionality": 9,
            "overall_score": 2.0, "feedback": "Solid.", "criteria_results": [],
        }, id_="t2")),
    ])
    result = EvaluatorAgent(api_cfg, client=client).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)

    assert result.passed is True and result.overall_score == pytest.approx(9.0)  # lying overall ignored
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "def login" in tool_result["content"]
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert {"read_file", "list_files", "search", "run_command", "submit_evaluation"} <= names


def test_api_evaluator_is_forced_to_submit_after_max_rounds(api_cfg):
    api_cfg.api_tool_rounds = 2
    client = FakeClient([
        _response(_tool_use("list_files", {})),
        _response(_tool_use("list_files", {}, id_="t2")),
        _response(_tool_use("submit_evaluation", {
            "design_quality": 5, "originality": 5, "craft": 5, "functionality": 5, "feedback": "meh",
            "criteria_results": [],
        }, id_="t3")),
    ])
    result = EvaluatorAgent(api_cfg, client=client).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)
    assert len(client.calls) == 3
    assert client.calls[-1]["tool_choice"] == {"type": "tool", "name": "submit_evaluation"}
    assert [t["name"] for t in client.calls[-1]["tools"]] == ["submit_evaluation"]
    assert result.overall_score == pytest.approx(5.0)


def test_api_evaluator_without_submission_is_parse_failure(api_cfg):
    api_cfg.api_tool_rounds = 0
    client = FakeClient([_response(SimpleNamespace(type="text", text="I refuse"))])
    result = EvaluatorAgent(api_cfg, client=client).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)
    assert result.parse_failed is True


def test_api_evaluator_bad_scores_are_parse_failure(api_cfg):
    api_cfg.api_tool_rounds = 0
    client = FakeClient([_response(_tool_use("submit_evaluation", {
        "design_quality": "great", "originality": 9, "craft": 9, "functionality": 9, "feedback": "x",
    }))])
    result = EvaluatorAgent(api_cfg, client=client).evaluate(Feature(id="f1", name="A", description="a"), "s", 1)
    assert result.parse_failed is True


def test_project_tools_boundaries(api_cfg, tmp_path):
    tools = ProjectTools(api_cfg)
    assert "def login" in tools.execute("read_file", {"path": "app.py"})
    assert "app.py" in tools.execute("list_files", {})
    assert "app.py:1: def login():" in tools.execute("search", {"pattern": r"def \w+"})
    assert tools.execute("read_file", {"path": "../outside.txt"}).startswith("ERROR: path is outside")
    assert "started" in tools.execute("run_command", {"command": api_cfg.startup_command})
    for bad in ("rm -rf /", "git push origin main", "git diff; rm -rf /", "python -c 'print(1)'"):
        assert tools.execute("run_command", {"command": bad}).startswith("ERROR:"), bad


@pytest.mark.skipif(sys.platform == "win32" and not Path("C:/Windows/System32/taskkill.exe").exists(),
                    reason="needs taskkill")
def test_run_command_stops_long_running_process(api_cfg):
    api_cfg.startup_command = f"{Path(sys.executable).as_posix()} -c \"import time; print('up', flush=True); time.sleep(60)\""
    tools = ProjectTools(api_cfg)
    tools.command_timeout = 2
    out = tools.execute("run_command", {"command": api_cfg.startup_command})
    assert out.startswith("timed out after 2s")
    assert "up" in out
