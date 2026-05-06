"""Detect what stage an arbitrary directory is at, then drive the right entry phase.

Used by `harness import` to take a directory (existing repo or empty folder)
and figure out where to plug it into the pipeline:

  HARNESS_PROJECT  → existing harness output; just call harness resume
  HAS_FEATURES     → features.json present; init phase will normalize, then loop
  HAS_SPEC         → spec.md exists, no features.json yet; skip planner alignment,
                     run initializer to decompose
  HAS_CODE         → has README/source but no harness artifacts; need brief +
                     planner alignment + init + loop
  EMPTY            → like `harness new` from scratch
  REVIEW_READY     → looks done (high feature pass rate, or substantial
                     code+tests+docs) → run ReviewerAgent only

The detector is pure: it reads file metadata only, never executes code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class EntryPhase(str, Enum):
    HARNESS_PROJECT = "harness_project"   # config.yaml present → harness resume
    HAS_FEATURES = "has_features"          # features.json present, no config
    HAS_SPEC = "has_spec"                  # spec.md present, no features.json
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


def detect_stage(
    project_dir: Path,
    *,
    review_pass_threshold: float = 0.8,
    config_filename: str = "config.yaml",
    features_filename: str = "features.json",
    spec_filename: str = "spec.md",
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
    spec_path = project_dir / spec_filename
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

    if spec_path.exists():
        report.entry_phase = EntryPhase.HAS_SPEC
        report.reasons.append(f"{spec_filename} present, no features yet → init only")
        if readme_path:
            report.suggested_name = _slug_to_name(project_dir.name)
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
