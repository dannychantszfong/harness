"""Notes on exhausted feature attempts (ADR-015, decision D-03).

When a feature uses up its iteration budget, Harness preserves the attempt
on a branch, reverts the working branch, and writes
`.harness/attempts/<feature-id>.md`. Any later attempt at that feature gets
the file injected into the generator prompt as lessons, so it does not start
from zero.
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Optional

from harness.progress.models import EvaluationResult, Feature
from harness.progress.tracker import attempts_file_relpath, write_atomic
from harness.timeutil import utcnow

# Cap on what is injected into a generator prompt.
LESSONS_MAX_CHARS = 8000

_DIMENSIONS = ("design_quality", "originality", "craft", "functionality")


def attempts_path(project_dir: Path, feature_id: str) -> Path:
    return Path(project_dir) / attempts_file_relpath(feature_id)


def write_attempt_notes(
    project_dir: Path,
    feature: Feature,
    *,
    attempt_number: int,
    max_iterations: int,
    branch: Optional[str],
    base_commit: Optional[str],
    attempt_commit: Optional[str],
    last_self_eval: Optional[str],
) -> Path:
    """Append one attempt's record to the feature's attempts file."""
    path = attempts_path(project_dir, feature.id)
    previous = path.read_text(encoding="utf-8") if path.exists() else (
        f"# Exhausted attempts — {feature.name} (`{feature.id}`)\n\n"
        f"{feature.description}\n"
    )
    history = feature.evaluation_history
    lines = [
        "",
        f"## Attempt {attempt_number} — recorded {utcnow().isoformat(timespec='seconds')}",
        "",
        f"- Iterations used: {len(history)} (budget {max_iterations})",
        f"- Code preserved on branch: `{branch}`" if branch else "- Code preserved on branch: (none — no git repo or no changes)",
    ]
    if attempt_commit:
        lines.append(f"- Attempt commit: `{attempt_commit}`")
    if base_commit:
        lines.append(f"- Working branch reverted to: `{base_commit}`")
    lines += ["", "### Failure pattern", "", *_failure_pattern(history), ""]
    lines += ["### Evaluations", ""]
    for result in history:
        lines += _evaluation_block(result)
    lines += ["### Generator's last self-evaluation", ""]
    lines.append((last_self_eval or "(none)").strip())
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (previous.rstrip("\n") + "\n" + "\n".join(lines)).encode("utf-8"))
    return path


def read_lessons(project_dir: Path, feature_id: str, max_chars: int = LESSONS_MAX_CHARS) -> Optional[str]:
    """The attempts file for a feature, truncated from the front to max_chars."""
    path = attempts_path(project_dir, feature_id)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if len(text) > max_chars:
        text = "…(earlier notes truncated)…\n" + text[-max_chars:]
    return text or None


def _evaluation_block(result: EvaluationResult) -> list[str]:
    header = (
        f"#### Iteration {result.iteration} — overall {result.overall_score:.2f} "
        f"(design {result.design_quality:.1f}, originality {result.originality:.1f}, "
        f"craft {result.craft:.1f}, functionality {result.functionality:.1f})"
    )
    if result.failure_reason:
        header += f" — {result.failure_reason}"
    feedback = result.feedback.strip()
    if len(feedback) > 1500:
        feedback = feedback[:1500] + " …"
    return [header, "", feedback, ""]


def _failure_pattern(history: list[EvaluationResult]) -> list[str]:
    graded = [r for r in history if not r.failure_reason]
    lines: list[str] = []
    if not graded:
        lines.append("- No iteration produced a usable grade.")
    else:
        averages = {d: mean(getattr(r, d) for r in graded) for d in _DIMENSIONS}
        weakest = min(averages, key=averages.get)
        lines.append(
            "- Average scores: "
            + ", ".join(f"{d.replace('_', ' ')} {v:.1f}" for d, v in averages.items())
        )
        lines.append(f"- Weakest dimension: **{weakest.replace('_', ' ')}**")
        overall = [round(r.overall_score, 1) for r in graded]
        lines.append(f"- Overall score by iteration: {', '.join(str(s) for s in overall)}")
    reasons: dict[str, int] = {}
    for r in history:
        if r.failure_reason:
            reasons[r.failure_reason] = reasons.get(r.failure_reason, 0) + 1
    for reason, count in sorted(reasons.items()):
        lines.append(f"- {count} iteration(s) failed without a grade: {reason}")
    return lines
