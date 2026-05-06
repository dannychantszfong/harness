"""Bare-bones tests for every agent class.

These would have caught:
  • GeneratorAgent abstract-method bug (couldn't even __init__)
  • Sprint contract crashing in runner mode (self.client was None)
  • Any agent that fails to honor orchestration_mode='runner' (no API key needed)

The principle: in runner mode, no agent should require an Anthropic client.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.agents import (
    InitializerAgent,
    PlannerAgent,
    GeneratorAgent,
    EvaluatorAgent,
)
from harness.config import HarnessConfig
from harness.progress.models import Feature


@pytest.fixture
def runner_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        project_name="t",
        brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
    )


@pytest.fixture
def api_config(tmp_path: Path, monkeypatch) -> HarnessConfig:
    # api mode requires the env var; provide a fake one so __init__ doesn't blow up
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    return HarnessConfig(
        project_name="t",
        brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="api",
    )


# ── Instantiability: catches abstract-method regressions ─────────────────────

def test_initializer_instantiates_in_runner_mode(runner_config):
    agent = InitializerAgent(runner_config)
    assert agent.client is None  # no Anthropic client in runner mode


def test_planner_instantiates_in_runner_mode(runner_config):
    agent = PlannerAgent(runner_config)
    assert agent.client is None


def test_evaluator_instantiates_in_runner_mode(runner_config):
    agent = EvaluatorAgent(runner_config)
    assert agent.client is None


def test_generator_instantiates_in_runner_mode(runner_config):
    runner = MagicMock()
    # If GeneratorAgent ever forgets to override `run`, this raises TypeError
    # ("Can't instantiate abstract class") — exactly the bug we hit.
    agent = GeneratorAgent(runner_config, runner=runner)
    assert agent.client is None
    assert agent.runner is runner


# ── _use_runner is wired correctly across all agents ─────────────────────────

def test_use_runner_flag_in_runner_mode(runner_config):
    assert InitializerAgent(runner_config)._use_runner is True
    assert PlannerAgent(runner_config)._use_runner is True
    assert EvaluatorAgent(runner_config)._use_runner is True
    assert GeneratorAgent(runner_config, runner=MagicMock())._use_runner is True


def test_use_runner_flag_in_api_mode(api_config):
    assert InitializerAgent(api_config)._use_runner is False
    assert PlannerAgent(api_config)._use_runner is False
    assert EvaluatorAgent(api_config)._use_runner is False
    assert GeneratorAgent(api_config, runner=MagicMock())._use_runner is False


# ── Sprint contract in runner mode (the AttributeError we hit) ───────────────

def test_sprint_contract_runner_mode_uses_default(runner_config):
    """negotiate_sprint_contract must not touch self.client when in runner mode."""
    agent = GeneratorAgent(runner_config, runner=MagicMock())
    feature = Feature(id="f1", name="Login", description="signup + login", priority=0)
    contract = agent.negotiate_sprint_contract(feature, spec="any spec text")
    assert contract.feature_id == "f1"
    assert "signup + login" in contract.acceptance_criteria
    assert contract.out_of_scope == []


# ── Run-method behavior on agents that delegate ──────────────────────────────

def test_generator_run_raises_helpful_message(runner_config):
    """Direct .run() should redirect callers to implement_feature()."""
    agent = GeneratorAgent(runner_config, runner=MagicMock())
    with pytest.raises(NotImplementedError) as exc:
        agent.run()
    assert "implement_feature" in str(exc.value)


def test_evaluator_run_raises_helpful_message(runner_config):
    agent = EvaluatorAgent(runner_config)
    with pytest.raises(NotImplementedError) as exc:
        agent.run()
    assert "evaluate" in str(exc.value)


# ── Runner-mode agents must not require ANTHROPIC_API_KEY ────────────────────

def test_runner_mode_agents_dont_require_api_key(tmp_path, monkeypatch):
    """The whole point of runner mode is no API key — pin this invariant."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = HarnessConfig(
        project_name="t", brief="b",
        output_dir=str(tmp_path),
        orchestration_mode="runner",
    )
    # All four agents must construct cleanly with no key in the environment
    InitializerAgent(config)
    PlannerAgent(config)
    EvaluatorAgent(config)
    GeneratorAgent(config, runner=MagicMock())
