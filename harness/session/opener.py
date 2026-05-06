"""Session opener — standardized startup sequence for each agent session.

From article 2, every session should:
1. Verify working directory / app can start
2. Read progress notes and git log
3. Review feature requirements
4. Run basic end-to-end tests
5. Select the next uncompleted feature

The SessionOpener builds the system prompt that instructs the agent to
perform this sequence before doing any implementation work.
"""

import subprocess
from pathlib import Path

from harness.config import HarnessConfig
from harness.progress.models import ProjectProgress
from harness.context.handoff import HandoffDocument


class SessionOpener:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def build_opening_context(
        self,
        progress: ProjectProgress,
        handoff: HandoffDocument | None,
        include_git_log: bool = True,
    ) -> str:
        """Return the full context block injected at the start of each session."""
        blocks: list[str] = []

        blocks.append(f"# Session {progress.session_count} — {progress.project_name}")
        blocks.append(f"")
        blocks.append(f"**Brief:** {progress.brief}")
        blocks.append(
            f"**Progress:** {progress.completion_pct}% "
            f"({len(progress.passing_features)}/{len(progress.features)} features)"
        )

        # Prior handoff
        if handoff:
            blocks.append("")
            blocks.append(handoff.to_prompt_block())
        else:
            blocks.append("")
            blocks.append("## First session — no prior handoff")
            blocks.append(
                f"Read features.json, run `{self.config.startup_command_for_platform}` "
                "to confirm the app starts, "
                "then pick the highest-priority pending feature."
            )

        # Git log for situational awareness
        if include_git_log:
            git_log = self._read_git_log()
            if git_log:
                blocks.append("")
                blocks.append("## Recent git history")
                blocks.append("```")
                blocks.append(git_log)
                blocks.append("```")

        # Feature status snapshot
        blocks.append("")
        blocks.append("## Feature status snapshot")
        for feature in sorted(progress.features, key=lambda f: f.priority):
            status_icon = {
                "pending": "⬜",
                "in_progress": "🔄",
                "passing": "✅",
                "failing": "❌",
            }.get(feature.status.value, "?")
            blocks.append(f"- {status_icon} [{feature.id}] {feature.name}")

        # Startup checklist
        blocks.append("")
        blocks.append("## Your startup checklist (complete before implementing anything)")
        blocks.append("1. `cd` to the project directory")
        blocks.append(
            f"2. Read `{self.config.spec_file}` — the confirmed product spec, "
            "the source of truth for what to build"
        )
        blocks.append(
            f"3. Run `{self.config.startup_command_for_platform}` — confirm the app starts"
        )
        blocks.append(f"4. Read `{self.config.features_file}` to understand remaining work")
        blocks.append("5. Run a quick smoke test in the browser (or via curl)")
        blocks.append("6. Pick the highest-priority `pending` feature and begin")

        return "\n".join(blocks)

    def _read_git_log(self) -> str:
        output_dir = Path(self.config.output_dir)
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-20"],
                cwd=output_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except Exception:
            return ""
