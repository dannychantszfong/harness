"""File-based handoff documents for cross-session context transfer.

Agents communicate through files rather than shared memory.  A HandoffDocument
captures everything the next agent needs to pick up where the previous one left
off — without relying on conversation history that evaporates across context resets.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json


@dataclass
class HandoffDocument:
    """Structured knowledge transfer between agent sessions."""

    project_name: str
    session_number: int
    completed_at: datetime
    what_was_done: str
    current_state: str
    next_action: str
    open_questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Raw path references so the next agent knows where to look
    key_files: dict[str, str] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """Render the handoff as a prompt-ready context block."""
        lines = [
            f"## Handoff from Session {self.session_number}",
            f"Completed at: {self.completed_at.isoformat()}",
            "",
            "### What was done",
            self.what_was_done,
            "",
            "### Current state",
            self.current_state,
            "",
            "### Your next action",
            self.next_action,
        ]
        if self.open_questions:
            lines += ["", "### Open questions"]
            lines += [f"- {q}" for q in self.open_questions]
        if self.warnings:
            lines += ["", "### Warnings"]
            lines += [f"- ⚠️  {w}" for w in self.warnings]
        if self.key_files:
            lines += ["", "### Key files"]
            for label, path in self.key_files.items():
                lines.append(f"- **{label}**: `{path}`")
        return "\n".join(lines)

    def save(self, directory: Path) -> Path:
        path = directory / f"handoff_session_{self.session_number:04d}.json"
        path.write_text(
            json.dumps(
                {
                    "project_name": self.project_name,
                    "session_number": self.session_number,
                    "completed_at": self.completed_at.isoformat(),
                    "what_was_done": self.what_was_done,
                    "current_state": self.current_state,
                    "next_action": self.next_action,
                    "open_questions": self.open_questions,
                    "warnings": self.warnings,
                    "key_files": self.key_files,
                },
                indent=2,
            )
        )
        return path

    @classmethod
    def load_latest(cls, directory: Path) -> "HandoffDocument | None":
        handoffs = sorted(directory.glob("handoff_session_*.json"))
        if not handoffs:
            return None
        data = json.loads(handoffs[-1].read_text())
        data["completed_at"] = datetime.fromisoformat(data["completed_at"])
        return cls(**data)
