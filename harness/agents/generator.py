"""Generator agent — implements one feature per session.

From the articles:
- Works on a SINGLE feature at a time to avoid context exhaustion.
- Negotiates a sprint contract with the evaluator before starting (article 1).
- Leaves code in a clean, commit-ready state after each feature.
- Performs a self-evaluation pass before handing off to the evaluator.

The actual implementation work is delegated to a CodeRunner — Claude Code
CLI (subprocess), Claude Code SDK (sdk) or Codex CLI (codex) — which writes
files and runs commands in the project directory.
"""

import re
from typing import Optional

from harness.agents.base import BaseAgent
from harness.progress.models import Feature, ProjectProgress, SprintContract
from harness.runners.base import (
    CodeRunner,
    RunnerFailedError,
    RunnerRateLimitedError,
    RunnerTimeoutError,
    RunnerTurnLimitError,
    RunResult,
)

_CONTRACT_SYSTEM = """You are proposing the sprint contract for one feature before implementation begins.
Define what "done" means for this feature only: specific, verifiable acceptance
criteria the evaluator can check by reading the code, running the app or the
tests — and what is explicitly out of scope for this sprint.
Keep it to this feature; do not restate the whole product spec.
"""

_RUNNER_CONTRACT_INSTRUCTION = """
Do not write code, modify files or install anything — this is planning only.
Read the project if it helps you scope the feature.

Output the contract in exactly this XML block (required):

<sprint_contract>
  <acceptance_criteria>
    <criterion>One specific, verifiable criterion</criterion>
    <criterion>…</criterion>
  </acceptance_criteria>
  <out_of_scope>
    <item>Something explicitly not built in this sprint</item>
  </out_of_scope>
</sprint_contract>
"""


def parse_sprint_contract(text: str, feature: Feature) -> Optional[SprintContract]:
    """The last <sprint_contract> block in text, or None if it has no criteria."""
    if not isinstance(text, str):
        return None
    blocks = re.findall(r"<sprint_contract>(.*?)</sprint_contract>", text, re.DOTALL)
    if not blocks:
        return None
    block = blocks[-1]

    def _items(section: str, tag: str) -> list[str]:
        m = re.search(rf"<{section}>(.*?)</{section}>", block, re.DOTALL)
        if not m:
            return []
        return [
            " ".join(item.split())
            for item in re.findall(rf"<{tag}>(.*?)</{tag}>", m.group(1), re.DOTALL)
            if item.strip()
        ]

    criteria = _items("acceptance_criteria", "criterion")
    if not criteria:
        return None
    return SprintContract(
        feature_id=feature.id,
        acceptance_criteria=criteria,
        out_of_scope=_items("out_of_scope", "item"),
    )


def _default_contract(feature: Feature) -> SprintContract:
    return SprintContract(
        feature_id=feature.id,
        acceptance_criteria=[feature.description],
        out_of_scope=[],
    )

_SPRINT_CONTRACT_TOOL = {
    "name": "propose_sprint_contract",
    "description": "Propose the sprint contract for this feature before implementation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific, verifiable criteria that constitute 'done'",
            },
            "out_of_scope": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things explicitly NOT being implemented in this sprint",
            },
        },
        "required": ["acceptance_criteria", "out_of_scope"],
    },
}


# Files Harness owns. Agents may read them but must never edit them (C-10);
# Harness restores features.json if a session changes it.
HARNESS_STATE_FILES_NOTE = (
    "Do not create, edit or delete Harness's state files — features.json, "
    "progress.md, harness_config.json, handoff_session_*.json and anything "
    "under .harness/. Harness owns them: changes to features.json and to the "
    "grading or permission settings in harness_config.json are reverted."
)


def _build_implementation_prompt(
    feature: Feature,
    session_preamble: str,
    evaluator_feedback: Optional[str],
    iteration: int,
    startup_command: str = "bash init.sh",
    lessons: Optional[str] = None,
) -> str:
    contract_block = ""
    if feature.sprint_contract:
        criteria = "\n".join(f"  - {c}" for c in feature.sprint_contract.acceptance_criteria)
        contract_block = f"\n\n**Sprint contract — acceptance criteria:**\n{criteria}"
        if feature.sprint_contract.out_of_scope:
            out = "\n".join(f"  - {c}" for c in feature.sprint_contract.out_of_scope)
            contract_block += f"\n\n**Out of scope for this feature:**\n{out}"

    lessons_block = ""
    if lessons:
        lessons_block = (
            "\n\n**Lessons from previous attempts at this feature** "
            "(those attempts ran out of iterations; their code was reverted and "
            "kept on a branch). Do not repeat what failed:\n\n"
            f"{lessons}"
        )

    feedback_block = ""
    if evaluator_feedback:
        feedback_block = (
            f"\n\n**Evaluator feedback from iteration {iteration - 1}:**\n"
            f"{evaluator_feedback}\n\n"
            "Address every piece of feedback before marking yourself done."
        )

    return (
        f"{session_preamble}\n\n"
        f"---\n\n"
        f"**Your task (iteration {iteration}):** Implement feature "
        f"`{feature.id}` — **{feature.name}**\n\n"
        f"{feature.description}"
        f"{contract_block}"
        f"{lessons_block}"
        f"{feedback_block}\n\n"
        f"{HARNESS_STATE_FILES_NOTE}\n\n"
        "When done:\n"
        f"1. Run `{startup_command}` and confirm the app starts.\n"
        "2. Write a self-evaluation: what you built, edge cases, concerns.\n"
        "3. Commit your work (Harness also records its own commit when the feature passes).\n"
        "4. Output your self-evaluation as the final message."
    )


class GeneratorAgent(BaseAgent):
    role = "generator"

    def __init__(self, config, runner: CodeRunner, **kwargs) -> None:
        super().__init__(config, **kwargs)
        self.runner = runner

    def run(self, **kwargs):
        raise NotImplementedError("Call implement_feature() directly.")

    def negotiate_sprint_contract(self, feature: Feature, spec: str) -> SprintContract:
        """Propose acceptance criteria before writing any code (ADR-006).

        API mode uses tool-use for structured output; runner mode asks the
        runner for a <sprint_contract> block (read-only session, C-06). If no
        usable contract comes back, the feature description is the contract.
        Disable the extra call with sprint_contract_enabled=false.
        """
        if self._use_runner:
            prompt = (
                f"{_CONTRACT_SYSTEM}\n"
                f"Feature `{feature.id}`: **{feature.name}**\n"
                f"Description: {feature.description}\n\n"
                f"Spec context:\n{spec[:4000]}\n"
                f"{_RUNNER_CONTRACT_INSTRUCTION}"
            )
            try:
                output = self._call_via_runner(prompt, access_role="contract")
            except RunnerRateLimitedError:
                raise
            except RuntimeError as exc:
                print(f"[generator] Sprint contract negotiation failed ({str(exc)[:200]}); "
                      "using the feature description as the contract.")
                return _default_contract(feature)
            contract = parse_sprint_contract(output, feature)
            if contract is None:
                print("[generator] No <sprint_contract> block in the answer; "
                      "using the feature description as the contract.")
                return _default_contract(feature)
            return contract

        messages = [
            {
                "role": "user",
                "content": (
                    f"Feature: **{feature.name}**\nDescription: {feature.description}\n\n"
                    f"Spec context:\n{spec[:4000]}\n\n"
                    "Use propose_sprint_contract to define what 'done' means."
                ),
            }
        ]
        _, tool_uses = self._call(
            system=_CONTRACT_SYSTEM,
            messages=messages,
            tools=[_SPRINT_CONTRACT_TOOL],
            stream=False,
        )
        if not tool_uses:
            return _default_contract(feature)
        inp = tool_uses[0]["input"]
        criteria = [str(c) for c in inp.get("acceptance_criteria") or [] if str(c).strip()]
        return SprintContract(
            feature_id=feature.id,
            acceptance_criteria=criteria or [feature.description],
            out_of_scope=[str(c) for c in inp.get("out_of_scope") or []],
        )

    def implement_feature(
        self,
        feature: Feature,
        progress: ProjectProgress,
        session_preamble: str,
        evaluator_feedback: Optional[str] = None,
        iteration: int = 1,
        lessons: Optional[str] = None,
    ) -> str:
        """Delegate implementation to the configured CodeRunner."""
        prompt = _build_implementation_prompt(
            feature=feature,
            session_preamble=session_preamble,
            evaluator_feedback=evaluator_feedback,
            iteration=iteration,
            startup_command=self.config.startup_command_for_platform,
            lessons=lessons,
        )

        result: RunResult = self.runner.implement(
            prompt=prompt,
            cwd=str(self.config.output_path),
            timeout_seconds=self._runner_timeout(),
            **self._access_kwargs(),
        )
        # Tokens were spent even when the session failed.
        self.usage.add_run_result(result)

        if not result.success:
            if result.rate_limited or result.rate_limit_reset_at is not None:
                raise RunnerRateLimitedError(
                    reset_at=result.rate_limit_reset_at,
                    raw_message=result.error or "",
                )
            if result.timed_out:
                # Not a self-evaluation: the orchestrator records a failed
                # iteration instead of asking the evaluator to grade it.
                raise RunnerTimeoutError(
                    timeout_seconds=self.config.runner_timeout_seconds,
                    raw_message=result.error or "",
                )
            if result.turn_limited:
                raise RunnerTurnLimitError(raw_message=result.error or "")
            # Anything else is the runner failing, not the agent: grading it
            # would spend the feature's budget on a broken runner (R-05).
            raise RunnerFailedError(raw_message=result.error or "runner session failed")

        return result.output or "(no output from runner)"
