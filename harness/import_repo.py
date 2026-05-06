"""Detect what stage an arbitrary directory is at, then drive the right entry phase.

Used by `harness import` to take a directory (existing repo or empty folder)
and figure out where to plug it into the pipeline:

  HARNESS_PROJECT  → existing harness output; just call harness resume
  HAS_FEATURES     → features.json present; init phase will normalize, then loop
  HAS_CODE         → has README/source but no harness artifacts; need brief +
                     planner alignment + init + loop
  EMPTY            → like `harness new` from scratch
  REVIEW_READY     → looks done (high feature pass rate, or substantial
                     code+tests+docs) → run ReviewerAgent only

The detector is pure: it reads file metadata only, never executes code. Whether
an arbitrary repo already contains a usable spec is assessed separately by the
selected coding agent during `harness import`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from harness.config import CONFIG_FILENAME
from harness.runners.base import CodeRunner, RunnerRateLimitedError


class EntryPhase(str, Enum):
    HARNESS_PROJECT = "harness_project"   # harness_config.json present → resume
    HAS_FEATURES = "has_features"          # features.json present, no config
    HAS_CODE = "has_code"                  # README/source, no harness artifacts
    EMPTY = "empty"                        # nothing meaningful → like harness new
    REVIEW_READY = "review_ready"          # looks finished → ReviewerAgent only


# Files that mark a directory as "has substantive code" rather than just notes.
_CODE_FILE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".rb", ".php", ".cs",
    ".ex", ".exs", ".clj", ".scala", ".dart",
}

# Files that indicate the project is set up for testing.
_TEST_HINTS = ("test_", "_test.", "tests/", "spec/", ".test.", ".spec.")

# Files that indicate CI is wired up.
_CI_PATHS = (".github/workflows", ".gitlab-ci.yml", "circle.yml",
             ".circleci/config.yml", "azure-pipelines.yml", "Jenkinsfile")


@dataclass
class StageReport:
    """What the detector saw and what entry phase it chose."""
    entry_phase: EntryPhase
    reasons: list[str] = field(default_factory=list)
    suggested_brief: Optional[str] = None
    suggested_name: Optional[str] = None
    feature_count: int = 0
    feature_pass_rate: float = 0.0     # 0..1; 0 if no features
    code_file_count: int = 0
    has_tests: bool = False
    has_ci: bool = False
    has_readme: bool = False


@dataclass
class RepoSpecAssessment:
    """Agent judgment about whether an imported repo already has a spec."""
    has_spec: bool = False
    spec_markdown: Optional[str] = None
    suggested_brief: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""


def detect_stage(
    project_dir: Path,
    *,
    review_pass_threshold: float = 0.8,
    config_filename: str = CONFIG_FILENAME,
    features_filename: str = "features.json",
) -> StageReport:
    """Probe the directory and decide where to enter the pipeline.

    Pure function — only reads file metadata, never runs code.
    """
    project_dir = project_dir.resolve()
    report = StageReport(entry_phase=EntryPhase.EMPTY)

    if not project_dir.exists() or not project_dir.is_dir():
        return report

    config_path = project_dir / config_filename
    features_path = project_dir / features_filename
    readme_path = _find_readme(project_dir)

    report.has_readme = readme_path is not None
    report.code_file_count = _count_code_files(project_dir)
    report.has_tests = _has_tests(project_dir)
    report.has_ci = _has_ci(project_dir)

    if config_path.exists():
        report.entry_phase = EntryPhase.HARNESS_PROJECT
        report.reasons.append(f"{config_filename} present — existing harness project")
        # Even harness projects can be 'review-ready' if (nearly) done.
        report.feature_count, report.feature_pass_rate = _features_summary(features_path)
        if (
            report.feature_count > 0
            and report.feature_pass_rate >= review_pass_threshold
        ):
            report.reasons.append(
                f"{report.feature_pass_rate:.0%} of {report.feature_count} "
                f"features passing — review-ready"
            )
            report.entry_phase = EntryPhase.REVIEW_READY
        return report

    if features_path.exists():
        report.entry_phase = EntryPhase.HAS_FEATURES
        report.feature_count, report.feature_pass_rate = _features_summary(features_path)
        report.reasons.append(
            f"{features_filename} present ({report.feature_count} features, "
            f"{report.feature_pass_rate:.0%} passing)"
        )
        if report.feature_pass_rate >= review_pass_threshold and report.feature_count > 0:
            report.entry_phase = EntryPhase.REVIEW_READY
            report.reasons.append("→ review-ready (pass-rate threshold)")
        return report

    # No harness artifacts — judge by the actual code state.
    if (
        report.code_file_count >= 5
        and report.has_readme
        and report.has_tests
    ):
        report.entry_phase = EntryPhase.REVIEW_READY
        report.reasons.append(
            f"{report.code_file_count} code files + README + tests, "
            f"no harness artifacts → review only"
        )
        report.suggested_brief = _read_brief_from_readme(readme_path) if readme_path else None
        report.suggested_name = _slug_to_name(project_dir.name)
        return report

    if report.code_file_count > 0 or readme_path:
        report.entry_phase = EntryPhase.HAS_CODE
        report.reasons.append(
            f"{report.code_file_count} code files"
            + (", README present" if readme_path else "")
            + " — needs spec + feature plan"
        )
        report.suggested_brief = _read_brief_from_readme(readme_path) if readme_path else None
        report.suggested_name = _slug_to_name(project_dir.name)
        return report

    report.entry_phase = EntryPhase.EMPTY
    report.reasons.append("Empty / minimal directory — start fresh")
    return report


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_readme(project_dir: Path) -> Optional[Path]:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = project_dir / name
        if p.exists():
            return p
    return None


def _count_code_files(project_dir: Path) -> int:
    """Cheap recursive count, capped to avoid pathological scans."""
    count = 0
    cap = 5000
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".pytest_cache", ".mypy_cache", "target",
    }
    for p in project_dir.rglob("*"):
        if count >= cap:
            break
        # Skip anything inside excluded directories
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.is_file() and p.suffix.lower() in _CODE_FILE_SUFFIXES:
            count += 1
    return count


def _has_tests(project_dir: Path) -> bool:
    if (project_dir / "tests").is_dir() or (project_dir / "test").is_dir():
        return True
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    seen = 0
    for p in project_dir.rglob("*"):
        if seen > 800:
            break
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.is_file():
            seen += 1
            name = p.name.lower()
            rel = str(p.relative_to(project_dir)).lower()
            if any(hint in rel or hint in name for hint in _TEST_HINTS):
                return True
    return False


def _has_ci(project_dir: Path) -> bool:
    return any((project_dir / sub).exists() for sub in _CI_PATHS)


def _features_summary(features_path: Path) -> tuple[int, float]:
    """Return (count, pass_rate). Tolerant of bare-list and canonical shapes."""
    if not features_path.exists():
        return 0, 0.0
    try:
        data = json.loads(features_path.read_text())
    except Exception:
        return 0, 0.0
    feats = data.get("features") if isinstance(data, dict) else data
    if not isinstance(feats, list) or not feats:
        return 0, 0.0
    passing = sum(1 for f in feats if f.get("status") == "passing")
    return len(feats), passing / len(feats)


def _read_brief_from_readme(readme_path: Path, max_chars: int = 500) -> Optional[str]:
    """Trim leading badges/empty lines, take the first prose paragraph."""
    try:
        text = readme_path.read_text()
    except Exception:
        return None
    paragraphs = []
    for chunk in text.split("\n\n"):
        line = chunk.strip()
        if not line:
            continue
        # Skip pure-badge / heading-only blocks
        if line.startswith("#") and len(line) < 80:
            continue
        if all(l.strip().startswith(("[", "<", "!")) for l in line.splitlines()):
            continue
        paragraphs.append(line)
        if sum(len(p) for p in paragraphs) >= max_chars:
            break
    if not paragraphs:
        return None
    return "\n\n".join(paragraphs)[:max_chars]


def _slug_to_name(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-")) or slug


def assess_repo_spec_with_agent(
    runner: CodeRunner,
    project_dir: Path,
    *,
    suggested_brief: str | None = None,
    timeout_seconds: int = 600,
) -> RepoSpecAssessment:
    """Ask the coding agent whether the imported repo already contains a spec."""
    prompt = _build_spec_assessment_prompt(suggested_brief=suggested_brief)
    result = runner.implement(prompt, cwd=str(project_dir), timeout_seconds=timeout_seconds)
    if not result.success:
        if result.rate_limited or result.rate_limit_reset_at is not None:
            raise RunnerRateLimitedError(
                reset_at=result.rate_limit_reset_at,
                raw_message=result.error or "",
            )
        return RepoSpecAssessment(reason=result.error or "repo assessment runner failed")
    return parse_repo_spec_assessment(result.output)


def parse_repo_spec_assessment(output: str) -> RepoSpecAssessment:
    """Parse the agent's tagged JSON response."""
    m = re.search(
        r"<harness_import_assessment>\s*(.*?)\s*</harness_import_assessment>",
        output,
        re.DOTALL,
    )
    payload = m.group(1) if m else output
    try:
        data = json.loads(payload.strip())
    except Exception:
        return RepoSpecAssessment(reason="Could not parse repo assessment JSON.")

    spec = data.get("spec_markdown")
    suggested = data.get("suggested_brief")
    return RepoSpecAssessment(
        has_spec=bool(data.get("has_spec") and isinstance(spec, str) and spec.strip()),
        spec_markdown=spec.strip() if isinstance(spec, str) and spec.strip() else None,
        suggested_brief=suggested.strip() if isinstance(suggested, str) and suggested.strip() else None,
        confidence=float(data.get("confidence") or 0.0),
        reason=str(data.get("reason") or ""),
    )


def _build_spec_assessment_prompt(suggested_brief: str | None = None) -> str:
    brief_hint = suggested_brief or "(none)"
    return f"""You are helping Agent Harness import an existing local repository.

Inspect the repository in the current working directory. Decide whether the repo
already contains enough product/specification material for Harness to skip
drafting a new product spec. Do not execute project code, install dependencies,
or modify files. Read docs, README, planning notes, and source structure as
needed.

A repo "has a spec" when it contains a durable description of intended product
behavior, scope, user workflows, acceptance criteria, or feature requirements.
It does not need to be named spec.md.

Suggested brief from heuristic README extraction:
{brief_hint}

Return only JSON wrapped in this exact tag:

<harness_import_assessment>
{{
  "has_spec": true,
  "confidence": 0.0,
  "reason": "short reason",
  "suggested_brief": "one short project brief",
  "spec_markdown": "# Product Specification\\n\\n..."
}}
</harness_import_assessment>

Rules:
- If the repo has scattered but usable spec material, synthesize it into
  spec_markdown with citations to source file paths.
- If it does not have a usable spec, set has_spec false and spec_markdown "".
- Keep spec_markdown product-focused: behavior, scope, flows, success criteria.
- Do not include implementation plans unless the repo's spec explicitly requires them.
"""
