"""Evaluator agent — GAN-style adversarial grader.

From article 1:
- Separates generation from evaluation to break self-evaluation bias.
- Grades on four criteria: design quality, originality, craft, functionality.
- Returns structured scores via tool use (api mode) or XML text parsing (runner mode).

Harness, not the model, decides the outcome (ADR-002, ADR-013):
- overall = Σ score_i × weight_i from the four sub-scores and the configured
  weights; a model-reported overall is logged for diagnostics only (P-05);
- a feature passes only if overall ≥ evaluator_pass_score AND
  functionality ≥ evaluator_functionality_floor (D-01);
- output Harness cannot read is an explicit parse failure, never a silent
  5.0 (P-07); scores are clamped to 1–10 (P-08).

The evaluator never writes code. It only observes and judges. In runner mode
its session is read-only (C-06); in api mode it inspects the project through
a bounded tool loop (C-05 / ADR-016).
"""

import re
from typing import Any, Optional

from rich.console import Console

from harness.agents.base import BaseAgent
from harness.progress.models import EvaluationResult, Feature
from harness.runners.base import RunnerRateLimitedError

console = Console()

DIMENSIONS: tuple[str, ...] = ("design_quality", "originality", "craft", "functionality")
_LABELS = {
    "design_quality": ("Design quality", "coherent mood, visual identity, intentional choices"),
    "originality": ("Originality", "custom decisions vs. boilerplate defaults"),
    "craft": ("Craft", "typography, spacing, color harmony, attention to detail"),
    "functionality": ("Functionality", "does it work correctly end-to-end?"),
}
_NUMBER = r"\d+(?:\.\d+)?"
SCORE_MIN, SCORE_MAX = 1.0, 10.0
# How much of an unparseable answer is kept for the log and for the retry.
RAW_TAIL_CHARS = 3000


def system_prompt(config) -> str:
    """Evaluator instructions with the configured weights and pass rule (P-06)."""
    weights = config.evaluator_weights
    criteria = "\n".join(
        f"{i}. **{_LABELS[d][0]}** — {_LABELS[d][1]} (weight: {getattr(weights, d):.0%})"
        for i, d in enumerate(DIMENSIONS, 1)
    )
    browser = (
        "\nUse the Playwright browser tools to exercise each acceptance criterion in the "
        "running app, and report what you observed.\n"
        if getattr(config, "evaluator_browser_verification", False)
        else ""
    )
    return f"""You are an adversarial QA evaluator grading AI-generated software.

Your job is NOT to be kind. It is to find every flaw so the generator can fix it.

Grading criteria (each scored 1-10):
{criteria}

Harness computes the weighted score itself. A feature passes only if the
weighted score is at least {config.evaluator_pass_score:g} AND functionality is at
least {config.evaluator_functionality_floor:g} — a feature that does not work never passes.

Process:
1. Read the sprint contract acceptance criteria.
2. Inspect the implementation: read the code, run the startup command
   (`{config.startup_command_for_platform}`) and the tests where you can.
3. Check each acceptance criterion against what you actually observed.
4. Score each dimension honestly.
5. Write specific, actionable feedback — not vague praise.
{browser}
You never write or modify code. Do not accept self-reported status from the generator.
"""


def runner_score_instruction(config) -> str:
    weights = config.evaluator_weights
    formula = " + ".join(f"{d}×{getattr(weights, d):g}" for d in DIMENSIONS)
    return f"""
After your evaluation, output your scores in this exact XML block (required):

<evaluation>
  <design_quality>X.X</design_quality>
  <originality>X.X</originality>
  <craft>X.X</craft>
  <functionality>X.X</functionality>
  <overall_score>X.X</overall_score>
  <feedback>Your specific, actionable feedback here (minimum 100 words).</feedback>
</evaluation>

Scores must be numbers between 1 and 10. Harness recomputes overall_score as
{formula}; pass threshold {config.evaluator_pass_score:g}, functionality floor
{config.evaluator_functionality_floor:g}.
"""


def compute_overall(scores: dict[str, float], weights) -> float:
    return sum(scores[d] * getattr(weights, d) for d in DIMENSIONS)


def build_result(
    scores: dict[str, float],
    feedback: str,
    iteration: int,
    config,
    reported_overall: Optional[float] = None,
) -> EvaluationResult:
    """Harness's verdict from four sub-scores (P-05, D-01)."""
    overall = round(compute_overall(scores, config.evaluator_weights), 4)
    if reported_overall is not None and abs(reported_overall - overall) > 0.05:
        console.print(
            f"    [dim]evaluator reported overall {reported_overall:.2f}; "
            f"Harness computed {overall:.2f} from the configured weights[/dim]"
        )
    passed = (
        overall >= config.evaluator_pass_score
        and scores["functionality"] >= config.evaluator_functionality_floor
    )
    return EvaluationResult(
        **scores,
        overall_score=overall,
        feedback=feedback,
        passed=passed,
        iteration=iteration,
        reported_overall=reported_overall,
    )


def parse_failure(iteration: int, raw: str, reason: str = "evaluator_unparseable") -> EvaluationResult:
    """An explicit "no usable grade" result (P-07) — never a fake 5.0."""
    tail = (raw or "").strip()[-RAW_TAIL_CHARS:]
    what = (
        "The evaluator session failed" if reason == "evaluator_error"
        else "The evaluator's answer did not contain a readable <evaluation> block"
    )
    feedback = f"[{what}; this iteration was not graded.]"
    if tail:
        feedback += f"\n\nEvaluator output (tail):\n{tail}"
    return EvaluationResult(
        design_quality=0.0,
        originality=0.0,
        craft=0.0,
        functionality=0.0,
        overall_score=0.0,
        feedback=feedback,
        passed=False,
        iteration=iteration,
        parse_failed=True,
        failure_reason=reason,
    )


def _clamp(dimension: str, value: float) -> float:
    if SCORE_MIN <= value <= SCORE_MAX:
        return value
    clamped = min(max(value, SCORE_MIN), SCORE_MAX)
    console.print(f"    [yellow]{dimension} score {value:g} is outside 1–10; clamped to {clamped:g}[/yellow]")
    return clamped


def _coerce_score(dimension: str, value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _clamp(dimension, float(value))
    if isinstance(value, str) and re.fullmatch(rf"\s*{_NUMBER}\s*", value):
        return _clamp(dimension, float(value))
    return None


def _parse_runner_scores(text: str, iteration: int, config) -> EvaluationResult:
    """Extract scores from the XML block the evaluator writes in runner mode."""
    blocks = re.findall(r"<evaluation>(.*?)</evaluation>", text or "", re.DOTALL)
    if not blocks:
        return parse_failure(iteration, text)
    block = blocks[-1]  # the answer, not a quoted template

    scores: dict[str, float] = {}
    for dimension in DIMENSIONS:
        m = re.search(rf"<{dimension}>\s*({_NUMBER})\s*</{dimension}>", block)
        if not m:
            return parse_failure(iteration, text)
        scores[dimension] = _clamp(dimension, float(m.group(1)))

    reported = re.search(rf"<overall_score>\s*({_NUMBER})\s*</overall_score>", block)
    feedback_match = re.search(r"<feedback>(.*?)</feedback>", block, re.DOTALL)
    feedback = (feedback_match.group(1).strip() if feedback_match else "") or (
        "(The evaluator gave scores but no written feedback.)"
    )
    return build_result(
        scores,
        feedback,
        iteration,
        config,
        reported_overall=float(reported.group(1)) if reported else None,
    )


def _eval_tool(config) -> dict:
    weights = config.evaluator_weights
    formula = " + ".join(f"{d}×{getattr(weights, d):g}" for d in DIMENSIONS)
    return {
        "name": "submit_evaluation",
        "description": (
            "Submit structured evaluation scores and feedback. Harness computes the "
            f"overall score as {formula}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **{d: {"type": "number", "minimum": 1, "maximum": 10} for d in DIMENSIONS},
                "overall_score": {
                    "type": "number",
                    "description": f"Your weighted average ({formula}); diagnostic only.",
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
            "required": [*DIMENSIONS, "feedback", "criteria_results"],
        },
    }


def _result_from_tool_input(inp: Optional[dict], iteration: int, config) -> EvaluationResult:
    if not isinstance(inp, dict):
        return parse_failure(iteration, "The evaluator did not call submit_evaluation.")
    scores: dict[str, float] = {}
    for dimension in DIMENSIONS:
        value = _coerce_score(dimension, inp.get(dimension))
        if value is None:
            return parse_failure(iteration, f"submit_evaluation input: {inp!r}")
        scores[dimension] = value
    reported = _coerce_score("overall_score", inp.get("overall_score")) if "overall_score" in inp else None
    feedback = str(inp.get("feedback") or "").strip() or "(The evaluator gave scores but no written feedback.)"
    return build_result(scores, feedback, iteration, config, reported_overall=reported)


class EvaluatorAgent(BaseAgent):
    role = "evaluator"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Raw answer of the last evaluate() call, for the one retry (P-07).
        self.last_raw_output: str = ""

    def run(self, **kwargs):
        raise NotImplementedError("Call evaluate() directly.")

    def evaluate(
        self,
        feature: Feature,
        generator_self_eval: str,
        iteration: int,
        previous_output: Optional[str] = None,
    ) -> EvaluationResult:
        """Grade the generator's output. Returns a structured EvaluationResult.

        previous_output: set on the single retry after an unparseable answer;
        the evaluator is asked to turn that analysis into the required block
        instead of starting over.
        """
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
            f"**Startup command:** `{self.config.startup_command_for_platform}`\n"
            + (f"**Test command:** `{self.config.test_command}`\n" if self.config.test_command else "")
            + f"\n**Generator self-evaluation (iteration {iteration}):**\n"
            f"{generator_self_eval}\n\n"
        )
        reminder = ""
        if previous_output is not None:
            reminder = (
                "\n\nIMPORTANT: your previous evaluation of this iteration ended without the "
                "required <evaluation> XML block (or with unreadable scores). Its output ended "
                "with:\n\n"
                f"{previous_output.strip()[-RAW_TAIL_CHARS:] or '(no output)'}\n\n"
                "Do not start over unless that analysis is unusable — output the required "
                "block now, with all four numeric scores."
            )

        # ── Runner mode: plain text prompt, parse XML response ─────────────
        if self._use_runner:
            prompt = (
                f"{system_prompt(self.config)}\n\n"
                f"{user_content}"
                "Evaluate the implementation. Run the startup command and test each acceptance "
                f"criterion. Then output your scores.\n{runner_score_instruction(self.config)}"
                f"{reminder}"
            )
            try:
                output = self._call_via_runner(prompt)
            except RunnerRateLimitedError:
                raise
            except RuntimeError as exc:
                self.last_raw_output = str(exc)
                console.print(f"    [yellow]Evaluator session failed: {str(exc)[:300]}[/yellow]")
                return parse_failure(iteration, str(exc), reason="evaluator_error")
            self.last_raw_output = output or ""
            result = _parse_runner_scores(self.last_raw_output, iteration, self.config)
            if result.parse_failed:
                self._log_unparseable(self.last_raw_output)
            return result

        # ── API mode: bounded inspection tool loop, then submit_evaluation ──
        messages = [
            {
                "role": "user",
                "content": (
                    user_content
                    + "Evaluate the implementation. Inspect the project with the tools — read "
                    "the code, run the allow-listed commands — and check each acceptance "
                    "criterion. Then call submit_evaluation."
                    + reminder
                ),
            }
        ]
        inp = self._call_with_tools(
            system=system_prompt(self.config),
            messages=messages,
            final_tool=_eval_tool(self.config),
        )
        self.last_raw_output = repr(inp) if inp is not None else ""
        result = _result_from_tool_input(inp, iteration, self.config)
        if result.parse_failed:
            self._log_unparseable(self.last_raw_output or "(no submit_evaluation call)")
        return result

    @staticmethod
    def _log_unparseable(raw: str) -> None:
        tail = (raw or "").strip()[-800:] or "(empty)"
        console.print(f"    [yellow]Evaluator output was not usable. Tail:[/yellow]\n[dim]{tail}[/dim]")
