"""Generator agent — implements one feature per session.

From the articles:
- Works on a SINGLE feature at a time to avoid context exhaustion.
- Negotiates a sprint contract with the evaluator before starting (article 1).
- Leaves code in a clean, commit-ready state after each feature.
- Performs a self-evaluation pass before handing off to the evaluator.

The actual implementation work is delegated to a CodeRunner, which can be:
  - subprocess / sdk / codex  → agentic, writes files, uses subscription
  - anthropic / openai / gemini / openrouter → API call, text output only
"""

from typing import Optional

from harness.agents.base import BaseAgent
from harness.progress.models import Feature, ProjectProgress, SprintContract
from harness.runners.base import CodeRunner, RunnerRateLimitedError, RunResult

_CONTRACT_SYSTEM = """You are reviewing a sprint contract before implementation begins.
Confirm you understand the acceptance criteria and flag anything ambiguous.
Reply with CONFIRMED: <criteria list> and CLARIFICATIONS NEEDED: <list or 'none'>
"""

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


def _build_implementation_prompt(
    feature: Feature,
    session_preamble: str,
    evaluator_feedback: Optional[str],
    iteration: int,
) -> str:
    contract_block = ""
    if feature.sprint_contract:
        criteria = "\n".join(f"  - {c}" for c in feature.sprint_contract.acceptance_criteria)
        contract_block = f"\n\n**Sprint contract — acceptance criteria:**\n{criteria}"

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
        f"{feedback_block}\n\n"
        "When done:\n"
        "1. Run init.sh and confirm the app starts.\n"
        "2. Write a self-evaluation: what you built, edge cases, concerns.\n"
        "3. git add -A && git commit -m 'feat: <feature name>'\n"
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
        """Propose acceptance criteria before writing any code.

        In API mode this uses tool-use for structured output. In runner mode
        we fall back to a simple default contract — running a full structured
        negotiation through a subscription runner isn't worth the round trip.
        """
        if self._use_runner:
            return SprintContract(
                feature_id=feature.id,
                acceptance_criteria=[feature.description],
                out_of_scope=[],
            )

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
            return SprintContract(
                feature_id=feature.id,
                acceptance_criteria=[feature.description],
                out_of_scope=[],
            )
        inp = tool_uses[0]["input"]
        return SprintContract(
            feature_id=feature.id,
            acceptance_criteria=inp.get("acceptance_criteria", [feature.description]),
            out_of_scope=inp.get("out_of_scope", []),
        )

    def implement_feature(
        self,
        feature: Feature,
        progress: ProjectProgress,
        session_preamble: str,
        evaluator_feedback: Optional[str] = None,
        iteration: int = 1,
    ) -> str:
        """Delegate implementation to the configured CodeRunner."""
        prompt = _build_implementation_prompt(
            feature=feature,
            session_preamble=session_preamble,
            evaluator_feedback=evaluator_feedback,
            iteration=iteration,
        )

        result: RunResult = self.runner.implement(
            prompt=prompt,
            cwd=str(self.config.output_path),
        )

        if not result.success:
            if result.rate_limited or result.rate_limit_reset_at is not None:
                raise RunnerRateLimitedError(
                    reset_at=result.rate_limit_reset_at,
                    raw_message=result.error or "",
                )
            return f"[Runner error] {result.error}\n\nPartial output:\n{result.output}"

        # Accumulate token usage when the runner provides it
        if result.input_tokens:
            self.usage.input_tokens += result.input_tokens
        if result.output_tokens:
            self.usage.output_tokens += result.output_tokens

        return result.output or "(no output from runner)"
