from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from harness.timeutil import UTCDateTime, utcnow


class FeatureStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSING = "passing"
    FAILING = "failing"


class EvaluationResult(BaseModel):
    design_quality: float
    originality: float
    craft: float
    functionality: float
    overall_score: float
    feedback: str
    passed: bool
    iteration: int
    evaluated_at: UTCDateTime = Field(default_factory=utcnow)
    # Set when the iteration failed before/without a real grade, e.g.
    # "generator_timeout". Such iterations still count against the budget.
    failure_reason: Optional[str] = None
    # True when the evaluator's answer could not be read (P-07); the scores
    # above are then 0, not a grade.
    parse_failed: bool = False
    # The overall the model itself reported — diagnostics only (P-05).
    reported_overall: Optional[float] = None


class SprintContract(BaseModel):
    """Negotiated success criteria between generator and evaluator before a sprint."""
    feature_id: str
    acceptance_criteria: list[str]
    out_of_scope: list[str]
    agreed_at: UTCDateTime = Field(default_factory=utcnow)


class Feature(BaseModel):
    id: str
    name: str
    description: str
    status: FeatureStatus = FeatureStatus.PENDING
    priority: int = 0  # lower = higher priority
    sprint_contract: Optional[SprintContract] = None
    evaluation_history: list[EvaluationResult] = Field(default_factory=list)
    implementation_notes: Optional[str] = None
    implemented_at: Optional[UTCDateTime] = None
    last_updated: UTCDateTime = Field(default_factory=utcnow)
    # Git recovery points (ADR-015). base_commit_sha is HEAD when the current
    # attempt started; commit_sha is the recovery point after it passed.
    base_commit_sha: Optional[str] = None
    commit_sha: Optional[str] = None
    # Branches preserving exhausted attempts: harness/attempt/<id>/<n>.
    attempt_branches: list[str] = Field(default_factory=list)
    # Usage spent on this feature (contract, generator, evaluator sessions).
    # cost_usd is the runtimes' own figure (API-equivalent on a subscription).
    tokens_used: int = 0
    cost_usd: float = 0.0

    @property
    def latest_evaluation(self) -> Optional[EvaluationResult]:
        if self.evaluation_history:
            return self.evaluation_history[-1]
        return None

    @property
    def iteration_count(self) -> int:
        return len(self.evaluation_history)

    def is_exhausted(self, max_iterations: int) -> bool:
        """Failed and out of iteration budget (derived; status stays FAILING)."""
        return self.status != FeatureStatus.PASSING and self.iteration_count >= max_iterations


@dataclass
class ResumeSummary:
    """What a (re)started feature loop will find, for C-09 and the final panel."""
    passing: list[Feature] = field(default_factory=list)
    in_progress: list[Feature] = field(default_factory=list)
    pending: list[Feature] = field(default_factory=list)   # incl. failing with budget left
    exhausted: list[Feature] = field(default_factory=list)
    next_feature: Optional[Feature] = None


class ProjectProgress(BaseModel):
    project_name: str
    brief: str
    spec: Optional[str] = None
    features: list[Feature] = Field(default_factory=list)
    current_feature_id: Optional[str] = None
    total_cost_usd: float = 0.0
    total_tokens_used: int = 0
    session_count: int = 0
    created_at: UTCDateTime = Field(default_factory=utcnow)
    last_updated: UTCDateTime = Field(default_factory=utcnow)

    @property
    def pending_features(self) -> list[Feature]:
        return [f for f in self.features if f.status == FeatureStatus.PENDING]

    @property
    def passing_features(self) -> list[Feature]:
        return [f for f in self.features if f.status == FeatureStatus.PASSING]

    @property
    def failing_features(self) -> list[Feature]:
        return [f for f in self.features if f.status == FeatureStatus.FAILING]

    @property
    def completion_pct(self) -> float:
        if not self.features:
            return 0.0
        passing = len(self.passing_features)
        return round(passing / len(self.features) * 100, 1)

    def get_feature(self, feature_id: str) -> Optional[Feature]:
        for f in self.features:
            if f.id == feature_id:
                return f
        return None

    def next_pending_feature(self) -> Optional[Feature]:
        pending = sorted(self.pending_features, key=lambda f: f.priority)
        return pending[0] if pending else None

    def next_actionable_feature(self, max_iterations: int) -> Optional[Feature]:
        """The feature the loop should work on next (P-02).

        An interrupted IN_PROGRESS feature resumes first; then PENDING and
        FAILING features that still have iteration budget, by priority.
        Exhausted features are never returned.
        """
        interrupted = sorted(
            (
                f for f in self.features
                if f.status == FeatureStatus.IN_PROGRESS and not f.is_exhausted(max_iterations)
            ),
            key=lambda f: f.priority,
        )
        if interrupted:
            return interrupted[0]
        queued = sorted(
            (
                f for f in self.features
                if f.status in (FeatureStatus.PENDING, FeatureStatus.FAILING)
                and not f.is_exhausted(max_iterations)
            ),
            key=lambda f: f.priority,
        )
        return queued[0] if queued else None

    def exhausted_features(self, max_iterations: int) -> list[Feature]:
        return [f for f in self.features if f.is_exhausted(max_iterations)]

    def resume_summary(self, max_iterations: int) -> ResumeSummary:
        summary = ResumeSummary(next_feature=self.next_actionable_feature(max_iterations))
        for f in sorted(self.features, key=lambda f: f.priority):
            if f.status == FeatureStatus.PASSING:
                summary.passing.append(f)
            elif f.is_exhausted(max_iterations):
                summary.exhausted.append(f)
            elif f.status == FeatureStatus.IN_PROGRESS:
                summary.in_progress.append(f)
            else:
                summary.pending.append(f)
        return summary


def dedupe_feature_ids(features: list[Feature]) -> list[tuple[str, str]]:
    """Rename duplicate ids in place (f1, f1-2, f1-3, …). Returns (old, new) pairs.

    Deterministic: the first occurrence keeps its id, later ones get the
    lowest free numeric suffix, so get_feature() never hits the wrong entry.
    """
    seen: set[str] = set()
    taken = {f.id for f in features}
    renames: list[tuple[str, str]] = []
    for feature in features:
        if feature.id not in seen:
            seen.add(feature.id)
            continue
        n = 2
        while f"{feature.id}-{n}" in taken:
            n += 1
        new_id = f"{feature.id}-{n}"
        renames.append((feature.id, new_id))
        feature.id = new_id
        taken.add(new_id)
        seen.add(new_id)
    return renames


def normalize_feature_entries(raw: Any) -> tuple[list[dict], list[str]]:
    """Validate runner-written feature entries (P-04).

    Accepts a list of dicts (or {"features": [...]}). Each entry needs a
    name or a description; a missing id becomes f<n>, a missing name or
    description borrows the other, a bad priority falls back to the list
    position. Unusable entries are dropped with a warning. Duplicate ids are
    resolved later by dedupe_feature_ids().
    """
    if isinstance(raw, dict) and isinstance(raw.get("features"), list):
        raw = raw["features"]
    if not isinstance(raw, list):
        raise ValueError("feature list must be a JSON array of feature objects")

    entries: list[dict] = []
    warnings: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"entry {idx + 1}: not an object — skipped")
            continue
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        if not name and not description:
            warnings.append(f"entry {idx + 1}: no name or description — skipped")
            continue
        feature_id = str(item.get("id") or "").strip()
        if not feature_id:
            feature_id = f"f{idx + 1}"
            warnings.append(f"entry {idx + 1}: missing id — assigned {feature_id}")
        if not name:
            name = description[:80]
            warnings.append(f"{feature_id}: missing name — using the description")
        if not description:
            description = name
            warnings.append(f"{feature_id}: missing description — using the name")
        try:
            priority = int(item.get("priority", idx))
        except (TypeError, ValueError):
            priority = idx
            warnings.append(f"{feature_id}: invalid priority {item.get('priority')!r} — using {idx}")
        entries.append({"id": feature_id, "name": name, "description": description, "priority": priority})

    if not entries:
        raise ValueError("feature list contains no usable features")
    return entries, warnings
