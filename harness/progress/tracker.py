import json
from pathlib import Path
from datetime import datetime

from harness.progress.models import (
    Feature,
    FeatureStatus,
    EvaluationResult,
    ProjectProgress,
    SprintContract,
)
from harness.config import HarnessConfig


class ProgressTracker:
    """Reads and writes project progress to disk.

    The JSON feature list is the single source of truth between sessions.
    Agents should read it at session start and write it after each feature.
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self._progress: ProjectProgress | None = None

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self) -> ProjectProgress:
        path = self.config.features_path
        if not path.exists():
            raise FileNotFoundError(
                f"Features file not found at {path}. Run the initializer first."
            )
        data = json.loads(path.read_text())
        self._progress = ProjectProgress.model_validate(data)
        return self._progress

    def save(self, progress: ProjectProgress) -> None:
        path = self.config.features_path
        path.parent.mkdir(parents=True, exist_ok=True)
        progress.last_updated = datetime.utcnow()
        path.write_text(progress.model_dump_json(indent=2))
        self._progress = progress
        self._write_markdown_summary(progress)

    def load_or_create(self, brief: str) -> ProjectProgress:
        if self.config.features_path.exists():
            return self.load()
        progress = ProjectProgress(
            project_name=self.config.project_name,
            brief=brief,
        )
        self.save(progress)
        return progress

    # ------------------------------------------------------------------
    # Feature mutations
    # ------------------------------------------------------------------

    def set_features(self, progress: ProjectProgress, features: list[Feature]) -> ProjectProgress:
        progress.features = features
        self.save(progress)
        return progress

    def mark_in_progress(self, progress: ProjectProgress, feature_id: str) -> ProjectProgress:
        feature = progress.get_feature(feature_id)
        if not feature:
            raise ValueError(f"Feature {feature_id} not found")
        feature.status = FeatureStatus.IN_PROGRESS
        feature.last_updated = datetime.utcnow()
        progress.current_feature_id = feature_id
        self.save(progress)
        return progress

    def record_evaluation(
        self,
        progress: ProjectProgress,
        feature_id: str,
        result: EvaluationResult,
    ) -> ProjectProgress:
        feature = progress.get_feature(feature_id)
        if not feature:
            raise ValueError(f"Feature {feature_id} not found")
        feature.evaluation_history.append(result)
        feature.status = FeatureStatus.PASSING if result.passed else FeatureStatus.FAILING
        feature.last_updated = datetime.utcnow()
        if result.passed:
            feature.implemented_at = datetime.utcnow()
            progress.current_feature_id = None
        self.save(progress)
        return progress

    def attach_sprint_contract(
        self,
        progress: ProjectProgress,
        feature_id: str,
        contract: SprintContract,
    ) -> ProjectProgress:
        feature = progress.get_feature(feature_id)
        if not feature:
            raise ValueError(f"Feature {feature_id} not found")
        feature.sprint_contract = contract
        feature.last_updated = datetime.utcnow()
        self.save(progress)
        return progress

    # ------------------------------------------------------------------
    # Human-readable summary
    # ------------------------------------------------------------------

    def _write_markdown_summary(self, progress: ProjectProgress) -> None:
        lines = [
            f"# {progress.project_name} — Progress",
            f"",
            f"**Brief:** {progress.brief}",
            f"**Completion:** {progress.completion_pct}% ({len(progress.passing_features)}/{len(progress.features)} features)",
            f"**Sessions:** {progress.session_count}",
            f"**Last updated:** {progress.last_updated.isoformat()}",
            f"",
            f"## Features",
            f"",
        ]
        for f in sorted(progress.features, key=lambda x: x.priority):
            icon = {
                FeatureStatus.PENDING: "⬜",
                FeatureStatus.IN_PROGRESS: "🔄",
                FeatureStatus.PASSING: "✅",
                FeatureStatus.FAILING: "❌",
            }[f.status]
            score_str = ""
            if f.latest_evaluation:
                score_str = f" (score: {f.latest_evaluation.overall_score:.1f}/10)"
            lines.append(f"- {icon} **{f.name}**{score_str} — {f.description}")

        self.config.progress_path.write_text("\n".join(lines))
