"""Evaluator agent — GAN-style adversarial grader.

From article 1:
- Separates generation from evaluation to break self-evaluation bias.
- Grades on four criteria: design quality, originality, craft, functionality.
- Uses Playwright (via MCP or subprocess) for interactive verification.
- Returns structured scores via tool use (api mode) or XML text parsing (runner mode).

The evaluator never writes code. It only observes and judges.
"""

import re

from harness.agents.base import BaseAgent
from harness.config import HarnessConfig
from harness.progress.models import Feature, EvaluationResult, SprintContract

_SYSTEM = """You are an adversarial QA evaluator grading AI-generated software.

Your job is NOT to be kind. It is to find every flaw so the generator can fix it.

Grading criteria (each scored 1-10):
1. **Design quality** — coherent mood, visual identity, intentional choices (weight: 30%)
2. **Originality** — custom decisions vs. boilerplate defaults (weight: 30%)
3. **Craft** — typography, spacing, color harmony, attention to detail (weight: 25%)
4. **Functionality** — does it work correctly end-to-end? (weight: 15%)

Process:
1. Read the sprint contract acceptance criteria.
2. Run init.sh to start the app.
3. Navigate the UI and test each acceptance criterion.
4. Score each dimension honestly.
5. Write specific, actionable feedback — not vague praise.
6. Call submit_evaluation with your results.

If you have access to browser automation (Playwright MCP), USE IT.
Do not accept self-reported status from the generator.
"""

# In runner mode the evaluator outputs XML that we parse instead of using tool use.
_RUNNER_SCORE_INSTRUCTION = """
After your evaluation, output your scores in this exact XML block (required):

<evaluation>
  <design_quality>X.X</design_quality>
  <originality>X.X</originality>
  <craft>X.X</craft>
  <functionality>X.X</functionality>
  <overall_score>X.X</overall_score>
  <feedback>Your specific, actionable feedback here (minimum 100 words).</feedback>
</evaluation>

Scores must be numbers between 1 and 10. overall_score = design*0.30 + originality*0.30 + craft*0.25 + functionality*0.15.
"""


def _parse_runner_scores(text: str, iteration: int, pass_score: float) -> EvaluationResult:
    """Extract scores from the XML block the evaluator writes in runner mode."""
    def _float(tag: str) -> float:
        m = re.search(rf"<{tag}>\s*([\d.]+)\s*</{tag}>", text)
        return float(m.group(1)) if m else 5.0

    def _str(tag: str) -> str:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else "No feedback provided."

    dq = _float("design_quality")
    or_ = _float("originality")
    cr = _float("craft")
    fn = _float("functionality")
    overall = _float("overall_score") or (dq * 0.30 + or_ * 0.30 + cr * 0.25 + fn * 0.15)
    feedback = _str("feedback")

    return EvaluationResult(
        design_quality=dq,
        originality=or_,
        craft=cr,
        functionality=fn,
        overall_score=overall,
        feedback=feedback,
        passed=overall >= pass_score,
        iteration=iteration,
    )


_EVAL_TOOL = {
    "name": "submit_evaluation",
    "description": "Submit structured evaluation scores and feedback.",
    "input_schema": {
        "type": "object",
        "properties": {
            "design_quality": {"type": "number", "minimum": 1, "maximum": 10},
            "originality": {"type": "number", "minimum": 1, "maximum": 10},
            "craft": {"type": "number", "minimum": 1, "maximum": 10},
            "functionality": {"type": "number", "minimum": 1, "maximum": 10},
            "overall_score": {
                "type": "number",
                "description": "Weighted average using: design*0.30 + originality*0.30 + craft*0.25 + functionality*0.15",
            },
            "feedback": {
                "type": "string",
                "description": "Specific, actionable feedback for the generator. Minimum 100 words.",
            },
            "criteria_results": {
                "type": "array",
                "description": "Pass/fail for each acceptance criterion from the sprint contract",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "notes": {"type": "string"},
                    },
                    "required": ["criterion", "passed"],
                },
            },
        },
        "required": [
            "design_quality",
            "originality",
            "craft",
            "functionality",
            "overall_score",
            "feedback",
            "criteria_results",
        ],
    },
}


class EvaluatorAgent(BaseAgent):
    role = "evaluator"

    def run(self, **kwargs):
        raise NotImplementedError("Call evaluate() directly.")

    def evaluate(
        self,
        feature: Feature,
        generator_self_eval: str,
        iteration: int,
    ) -> EvaluationResult:
        """Grade the generator's output. Returns a structured EvaluationResult."""

        contract_block = ""
        if feature.sprint_contract:
            criteria = "\n".join(
                f"- {c}" for c in feature.sprint_contract.acceptance_criteria
            )
            out_of_scope = "\n".join(
                f"- {c}" for c in feature.sprint_contract.out_of_scope
            )
            contract_block = (
                f"\n\n**Sprint contract — acceptance criteria:**\n{criteria}\n\n"
                f"**Out of scope (do not penalize for missing):**\n{out_of_scope}"
            )

        user_content = (
            f"**Feature under review:** {feature.name}\n"
            f"**Description:** {feature.description}"
            f"{contract_block}\n\n"
            f"**Generator self-evaluation (iteration {iteration}):**\n"
            f"{generator_self_eval}\n\n"
        )

        # ── Runner mode: plain text prompt, parse XML response ─────────────
        if self._use_runner:
            prompt = (
                f"{_SYSTEM}\n\n"
                f"{user_content}"
                "Evaluate the implementation. Run init.sh and test each acceptance "
                f"criterion. Then output your scores.\n{_RUNNER_SCORE_INSTRUCTION}"
            )
            output = self._call_via_runner(prompt)
            return _parse_runner_scores(output, iteration, self.config.evaluator_pass_score)

        # ── API mode: tool use ──────────────────────────────────────────────
        messages = [
            {
                "role": "user",
                "content": (
                    user_content
                    + "Evaluate the implementation. Start by running init.sh and "
                    "testing each acceptance criterion. Then call submit_evaluation."
                ),
            }
        ]

        _, tool_uses = self._call(
            system=_SYSTEM,
            messages=messages,
            tools=[_EVAL_TOOL],
            stream=False,
            max_tokens=8096,
        )

        if not tool_uses:
            return EvaluationResult(
                design_quality=5.0,
                originality=5.0,
                craft=5.0,
                functionality=5.0,
                overall_score=5.0,
                feedback="Evaluator did not return structured output. Manual review required.",
                passed=False,
                iteration=iteration,
            )

        inp = tool_uses[0]["input"]
        weights = self.config.evaluator_weights
        computed_score = (
            inp["design_quality"] * weights.design_quality
            + inp["originality"] * weights.originality
            + inp["craft"] * weights.craft
            + inp["functionality"] * weights.functionality
        )
        overall = inp.get("overall_score", computed_score)

        return EvaluationResult(
            design_quality=inp["design_quality"],
            originality=inp["originality"],
            craft=inp["craft"],
            functionality=inp["functionality"],
            overall_score=overall,
            feedback=inp["feedback"],
            passed=overall >= self.config.evaluator_pass_score,
            iteration=iteration,
        )
