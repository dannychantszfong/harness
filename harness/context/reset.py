"""Context reset management.

Context resets outperform compaction for long-running tasks: they eliminate
context anxiety (the model wrapping up prematurely near token limits) and
allow fresh sessions to start without accumulated noise.

A reset produces a HandoffDocument so the new session has full situational
awareness without inheriting any of the old conversation.
"""

from datetime import datetime
from pathlib import Path

from harness.context.handoff import HandoffDocument
from harness.config import HarnessConfig
from harness.progress.models import ProjectProgress


class ContextReset:
    """Determines when to reset and constructs the handoff for the next session."""

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def should_reset(self, token_count: int) -> bool:
        return token_count >= self.config.context_reset_threshold_tokens

    def build_handoff(
        self,
        progress: ProjectProgress,
        session_number: int,
        what_was_done: str,
        current_state: str,
        next_action: str,
        open_questions: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> HandoffDocument:
        handoff = HandoffDocument(
            project_name=progress.project_name,
            session_number=session_number,
            completed_at=datetime.utcnow(),
            what_was_done=what_was_done,
            current_state=current_state,
            next_action=next_action,
            open_questions=open_questions or [],
            warnings=warnings or [],
            key_files={
                "features": str(self.config.features_path),
                "progress": str(self.config.progress_path),
                "init_script": str(self.config.init_script_path),
            },
        )
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        handoff.save(output_dir)
        return handoff

    @staticmethod
    def format_session_preamble(
        handoff: HandoffDocument | None,
        progress: ProjectProgress,
    ) -> str:
        """Build the opening system context for a fresh agent session."""
        blocks = [
            f"# Session Start — {progress.project_name}",
            "",
            f"**Brief:** {progress.brief}",
            f"**Progress:** {progress.completion_pct}% complete "
            f"({len(progress.passing_features)}/{len(progress.features)} features passing)",
            "",
        ]
        if handoff:
            blocks.append(handoff.to_prompt_block())
        else:
            blocks += [
                "## First session",
                "No prior handoff exists. Read the features file, run the "
                "configured startup command, confirm the app starts, then pick the highest-priority "
                "pending feature to implement.",
            ]
        next_feature = progress.next_pending_feature()
        if next_feature:
            blocks += [
                "",
                f"## Next target feature",
                f"**{next_feature.name}** (id: `{next_feature.id}`)",
                next_feature.description,
            ]
        return "\n".join(blocks)
