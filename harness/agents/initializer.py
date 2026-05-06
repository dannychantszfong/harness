"""Initializer agent — runs exactly once per project.

Responsibilities (from article 2):
- Create init.sh  (standardizes app startup; enables quick health checks)
- Create features.json (JSON feature list, all initially 'pending')
- Create progress.md  (human-readable summary)
- Write an initial git commit so all subsequent sessions have a clean baseline
"""

import json
import re
import subprocess
from pathlib import Path
from datetime import datetime

from harness.agents.base import BaseAgent
from harness.config import HarnessConfig
from harness.progress.models import Feature, FeatureStatus, ProjectProgress
from harness.progress.tracker import ProgressTracker

_SYSTEM = """You are a senior software architect initializing a new project harness.
Your job is to:
1. Decompose the project brief into a concrete, ordered feature list (JSON).
2. Write an init.sh script that starts the application cleanly.
3. Write a concise progress.md describing the current state.

Be exhaustive with features — the article recommends 200+ for large apps.
Features should be atomic and independently verifiable.
"""

_FEATURE_TOOL = {
    "name": "set_feature_list",
    "description": "Output the complete ordered feature list for this project.",
    "input_schema": {
        "type": "object",
        "properties": {
            "features": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "priority": {"type": "integer", "description": "Lower = higher priority"},
                    },
                    "required": ["id", "name", "description", "priority"],
                },
            },
            "init_sh": {
                "type": "string",
                "description": "Shell script content that starts the application",
            },
        },
        "required": ["features", "init_sh"],
    },
}


class InitializerAgent(BaseAgent):
    role = "planner"

    def run(self, brief: str, spec: str | None = None) -> ProjectProgress:
        tracker = ProgressTracker(self.config)
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        spec_text = spec
        if spec_text is None and self.config.spec_path.exists():
            spec_text = self.config.spec_path.read_text()
        planning_source = (
            f"Project specification:\n{spec_text}"
            if spec_text else f"Project brief: {brief}"
        )

        # features.json may already exist in two shapes:
        #  - canonical ProjectProgress dict (this harness wrote it)
        #  - bare list of feature dicts (an agentic runner wrote it as a side effect)
        # Either way, skip re-decomposition and normalize if needed.
        if self.config.features_path.exists():
            existing = self._load_or_promote_existing(tracker, brief)
            if existing is not None:
                # _load_or_promote_existing prints a "Promoted..." line when
                # it had to normalize a bare list. Otherwise we say so here.
                return existing

        if self._use_runner:
            raw_features, init_sh = self._decompose_via_runner(planning_source)
        else:
            raw_features, init_sh = self._decompose_via_api(planning_source)

        # ── shared: build Feature objects ──────────────────────────────────

        features = [
            Feature(
                id=f["id"],
                name=f["name"],
                description=f["description"],
                priority=f.get("priority", idx),
                status=FeatureStatus.PENDING,
            )
            for idx, f in enumerate(raw_features)
        ]

        progress = ProjectProgress(
            project_name=self.config.project_name,
            brief=brief,
            spec=spec_text,
            features=features,
            session_count=1,
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow(),
        )
        tracker.save(progress)

        init_path = self.config.init_script_path
        init_path.write_text(init_sh)
        init_path.chmod(0o755)

        self._initial_git_commit(output_dir)

        print(
            f"[initializer] Created {len(features)} features, "
            f"init.sh, and initial git commit."
        )
        return progress

    def _decompose_via_api(self, planning_source: str) -> tuple[list[dict], str]:
        """Use Anthropic API tool-use to get a structured feature list."""
        messages = [
            {
                "role": "user",
                "content": (
                    f"{planning_source}\n\n"
                    "Please decompose this into a feature list and write an init.sh. "
                    "Use the set_feature_list tool to output your answer."
                ),
            }
        ]
        _, tool_uses = self._call(
            system=_SYSTEM,
            messages=messages,
            tools=[_FEATURE_TOOL],
            stream=False,
        )
        if not tool_uses:
            raise RuntimeError("Initializer did not call set_feature_list tool")
        inp = tool_uses[0]["input"]
        return inp["features"], inp.get("init_sh", "#!/bin/bash\necho 'No init script provided'")

    def _decompose_via_runner(self, planning_source: str) -> tuple[list[dict], str]:
        """Use the runner to decompose the brief.

        Agentic runners (Claude Code, Codex) prefer to write files directly,
        so the prompt asks for that. We also accept the inline-tagged format
        as a fallback for runners that just return text.
        """
        prompt = (
            f"{_SYSTEM}\n\n"
            f"{planning_source}\n\n"
            "Your task here is *only* to plan — do not implement any features yet, "
            "do not create source code, do not install dependencies.\n\n"
            "Write exactly two files in the current working directory:\n\n"
            "  1. features.json — a JSON array (NOT an object) of features. "
            "Each entry must have these exact fields:\n"
            '       {\"id\": \"f1\", \"name\": \"...\", \"description\": \"...\", \"priority\": 0}\n'
            "     Lower priority = higher importance. Be exhaustive — aim for 50-200 atomic features.\n\n"
            "  2. init.sh — a bash script that starts the application cleanly "
            "(can be a placeholder echo if there's nothing to start yet).\n\n"
            "After writing both files, print the single line `INIT_DONE` and stop. "
            "If for some reason you cannot write files, output the contents inline "
            "wrapped in <features>...</features> and <init_sh>...</init_sh> tags."
        )
        output = self._call_via_runner(prompt)

        feat_path = self.config.features_path
        init_path = self.config.init_script_path

        # Preferred path: the runner wrote files directly to disk.
        if feat_path.exists():
            raw = json.loads(feat_path.read_text())
            if isinstance(raw, dict) and "features" in raw:
                raw = raw["features"]
            init_sh = (
                init_path.read_text()
                if init_path.exists()
                else "#!/bin/bash\necho 'No init script provided'"
            )
            return raw, init_sh

        # Fallback: parse inline-tagged blocks from the runner's stdout.
        features_match = re.search(r"<features>\s*(.*?)\s*</features>", output, re.DOTALL)
        init_match = re.search(r"<init_sh>\s*(.*?)\s*</init_sh>", output, re.DOTALL)

        if features_match:
            raw_features = json.loads(features_match.group(1))
            init_sh = (
                init_match.group(1).strip()
                if init_match
                else "#!/bin/bash\necho 'No init script provided'"
            )
            return raw_features, init_sh

        raise RuntimeError(
            "Initializer runner produced no features.json on disk and no "
            "<features> block in its output. "
            f"Last 500 chars of runner output:\n{output[-500:]}"
        )

    def _load_or_promote_existing(
        self, tracker: ProgressTracker, brief: str
    ) -> ProjectProgress | None:
        """Return existing progress, normalizing a bare-list shape if needed.

        Agentic runners sometimes write features.json as a plain list rather
        than the ProjectProgress envelope. We promote that to canonical shape
        on first read so downstream code only ever sees one format.
        """
        try:
            progress = tracker.load()
            print(
                f"[initializer] Loaded existing features.json "
                f"({len(progress.features)} features); skipping decomposition."
            )
            return progress
        except Exception:
            pass

        try:
            data = json.loads(self.config.features_path.read_text())
        except Exception:
            return None

        if not isinstance(data, list):
            return None

        features = [
            Feature(
                id=f["id"],
                name=f["name"],
                description=f["description"],
                priority=f.get("priority", idx),
                status=FeatureStatus.PENDING,
            )
            for idx, f in enumerate(data)
        ]
        progress = ProjectProgress(
            project_name=self.config.project_name,
            brief=brief,
            spec=self.config.spec_path.read_text() if self.config.spec_path.exists() else None,
            features=features,
            session_count=1,
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow(),
        )
        tracker.save(progress)
        print(
            f"[initializer] Promoted bare-list features.json "
            f"({len(features)} features) to canonical shape."
        )
        return progress

    def _initial_git_commit(self, directory: Path) -> None:
        try:
            subprocess.run(["git", "init"], cwd=directory, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=directory, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "chore: harness initialization"],
                cwd=directory,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            # Not fatal — git may already be initialized or not available
            print("[initializer] Warning: could not create initial git commit")
