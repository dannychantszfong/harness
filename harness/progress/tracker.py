import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from pydantic import ValidationError
from rich.console import Console

from harness.progress.models import (
    Feature,
    FeatureStatus,
    EvaluationResult,
    ProjectProgress,
    SprintContract,
    dedupe_feature_ids,
)
from harness.config import HarnessConfig
from harness.timeutil import utcnow

console = Console()


class CorruptFeaturesFileError(RuntimeError):
    """features.json exists but is not a usable progress file.

    Harness never overwrites it (P-03): the caller backs it up and aborts so
    the user can repair or restore it.
    """

    def __init__(self, path: Path, detail: str, backup: Optional[Path] = None):
        self.path = path
        self.detail = detail
        self.backup = backup
        message = f"{path} is not a valid Harness progress file: {detail}"
        if backup is not None:
            message += (
                f"\nA copy was saved to {backup}. Harness will not overwrite your "
                "progress: fix the file (or restore it from git: "
                f"`git -C {path.parent} checkout -- {path.name}`), then resume."
            )
        super().__init__(message)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace path with data without ever leaving a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_corrupt_file(path: Path) -> Path:
    """Copy path to <name>.corrupt-<timestamp> next to it; return the copy."""
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    shutil.copy2(path, backup)
    return backup


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
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorruptFeaturesFileError(path, f"invalid JSON ({exc})") from exc
        try:
            progress = ProjectProgress.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            where = ".".join(str(p) for p in first.get("loc", ())) or "root"
            raise CorruptFeaturesFileError(
                path, f"{where}: {first.get('msg', 'validation failed')}"
            ) from exc
        renames = dedupe_feature_ids(progress.features)
        if renames:
            console.print(
                "[yellow]features.json had duplicate feature ids; renamed "
                + ", ".join(f"{old} → {new}" for old, new in renames)
                + ".[/yellow]"
            )
            self.save(progress)
        self._progress = progress
        return progress

    def save(self, progress: ProjectProgress) -> None:
        path = self.config.features_path
        progress.last_updated = utcnow()
        write_atomic(path, progress.model_dump_json(indent=2).encode("utf-8"))
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
    # Protection against agents editing harness state (C-10)
    # ------------------------------------------------------------------

    @contextmanager
    def protect_state(self) -> Iterator[None]:
        """Restore features.json if an agent session changed it.

        Agents work in the project directory with write access; Harness is
        the only writer of its own state. Snapshot before the call, compare
        after (also when the call raised), restore the known-good copy.
        """
        path = self.config.features_path
        before = path.read_bytes() if path.exists() else None
        try:
            yield
        finally:
            if before is not None:
                after = path.read_bytes() if path.exists() else None
                if after != before:
                    write_atomic(path, before)
                    console.print(
                        "[yellow]⚠  An agent modified features.json "
                        f"(sha256 {_digest(before)} → {_digest(after)}); "
                        "restored Harness's copy.[/yellow]"
                    )

    # ------------------------------------------------------------------
    # Feature mutations
    # ------------------------------------------------------------------

    def set_features(self, progress: ProjectProgress, features: list[Feature]) -> ProjectProgress:
        progress.features = features
        self.save(progress)
        return progress

    def mark_in_progress(
        self,
        progress: ProjectProgress,
        feature_id: str,
        base_commit_sha: Optional[str] = None,
    ) -> ProjectProgress:
        feature = self._feature(progress, feature_id)
        feature.status = FeatureStatus.IN_PROGRESS
        feature.last_updated = utcnow()
        # Keep the original pre-feature state across interruptions/resumes.
        if feature.base_commit_sha is None and base_commit_sha:
            feature.base_commit_sha = base_commit_sha
        progress.current_feature_id = feature_id
        self.save(progress)
        return progress

    def record_evaluation(
        self,
        progress: ProjectProgress,
        feature_id: str,
        result: EvaluationResult,
    ) -> ProjectProgress:
        feature = self._feature(progress, feature_id)
        feature.evaluation_history.append(result)
        feature.status = FeatureStatus.PASSING if result.passed else FeatureStatus.FAILING
        feature.last_updated = utcnow()
        if result.passed:
            feature.implemented_at = utcnow()
            progress.current_feature_id = None
        self.save(progress)
        return progress

    def record_commit(
        self,
        progress: ProjectProgress,
        feature_id: str,
        commit_sha: Optional[str],
    ) -> ProjectProgress:
        feature = self._feature(progress, feature_id)
        feature.commit_sha = commit_sha
        self.save(progress)
        return progress

    def record_exhausted_attempt(
        self,
        progress: ProjectProgress,
        feature_id: str,
        branch: Optional[str],
    ) -> ProjectProgress:
        feature = self._feature(progress, feature_id)
        if branch and branch not in feature.attempt_branches:
            feature.attempt_branches.append(branch)
        feature.status = FeatureStatus.FAILING
        # The next attempt (raised budget, edited description) starts from
        # whatever is HEAD then, not from the reverted base.
        feature.base_commit_sha = None
        feature.last_updated = utcnow()
        if progress.current_feature_id == feature_id:
            progress.current_feature_id = None
        self.save(progress)
        return progress

    def add_usage(
        self,
        progress: ProjectProgress,
        feature_id: Optional[str],
        tokens: int,
        cost_usd: float,
    ) -> ProjectProgress:
        """Accumulate session usage into the project (and feature) totals (C-04)."""
        if not tokens and not cost_usd:
            return progress
        progress.total_tokens_used += int(tokens)
        progress.total_cost_usd += float(cost_usd)
        feature = progress.get_feature(feature_id) if feature_id else None
        if feature is not None:
            feature.tokens_used += int(tokens)
            feature.cost_usd += float(cost_usd)
        self.save(progress)
        return progress

    def attach_sprint_contract(
        self,
        progress: ProjectProgress,
        feature_id: str,
        contract: SprintContract,
    ) -> ProjectProgress:
        feature = self._feature(progress, feature_id)
        feature.sprint_contract = contract
        feature.last_updated = utcnow()
        self.save(progress)
        return progress

    @staticmethod
    def _feature(progress: ProjectProgress, feature_id: str) -> Feature:
        feature = progress.get_feature(feature_id)
        if not feature:
            raise ValueError(f"Feature {feature_id} not found")
        return feature

    # ------------------------------------------------------------------
    # Human-readable summary
    # ------------------------------------------------------------------

    def _write_markdown_summary(self, progress: ProjectProgress) -> None:
        max_iterations = self.config.max_iterations_per_feature
        lines = [
            f"# {progress.project_name} — Progress",
            "",
            f"**Brief:** {progress.brief}",
            f"**Completion:** {progress.completion_pct}% ({len(progress.passing_features)}/{len(progress.features)} features)",
            f"**Sessions:** {progress.session_count}",
            f"**Usage:** {usage_summary(progress.total_tokens_used, progress.total_cost_usd, self.config)}",
            f"**Last updated:** {progress.last_updated.isoformat()}",
            "",
            "## Features",
            "",
        ]
        for f in sorted(progress.features, key=lambda x: x.priority):
            icon = {
                FeatureStatus.PENDING: "⬜",
                FeatureStatus.IN_PROGRESS: "🔄",
                FeatureStatus.PASSING: "✅",
                FeatureStatus.FAILING: "❌",
            }[f.status]
            details = []
            if f.latest_evaluation:
                details.append(f"score: {f.latest_evaluation.overall_score:.1f}/10")
            if f.tokens_used:
                details.append(f"{f.tokens_used:,} tokens")
            detail_str = f" ({', '.join(details)})" if details else ""
            lines.append(f"- {icon} **{f.name}**{detail_str} — {f.description}")

        exhausted = progress.exhausted_features(max_iterations)
        if exhausted:
            lines += ["", "## Exhausted features", ""]
            lines.append(
                f"These features used their {max_iterations}-iteration budget without "
                "passing. Their code was reverted; the attempt is preserved on a branch."
            )
            lines.append("")
            for f in exhausted:
                branch = f.attempt_branches[-1] if f.attempt_branches else "(no branch)"
                lines.append(
                    f"- **{f.name}** (`{f.id}`) — notes: `{attempts_file_relpath(f.id)}`, "
                    f"branch: `{branch}`"
                )

        write_atomic(self.config.progress_path, "\n".join(lines).encode("utf-8"))


def usage_summary(tokens: int, cost_usd: float, config: HarnessConfig) -> str:
    """'123,456 tokens · cost: subscription (≈$1.23 at API prices)' and friends."""
    text = f"{tokens:,} tokens"
    if config.billing_mode == "subscription":
        text += " · cost: subscription"
        if cost_usd:
            text += f" (≈${cost_usd:,.2f} at API prices)"
    elif cost_usd:
        text += f" · ${cost_usd:,.2f}"
    else:
        text += " · cost: not reported"
    return text


def attempts_file_relpath(feature_id: str) -> str:
    """Where the lessons from exhausted attempts of a feature are recorded."""
    return f".harness/attempts/{_safe_name(feature_id)}.md"


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value) or "feature"


def _digest(data: Optional[bytes]) -> str:
    if data is None:
        return "deleted"
    return hashlib.sha256(data).hexdigest()[:12]
