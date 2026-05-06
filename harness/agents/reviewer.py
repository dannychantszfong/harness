"""Reviewer agent — audits an imported or finished project.

Distinct from Evaluator (per-feature scoring). Reviewer takes a whole
project and produces REVIEW.md: a punch list of gaps in documentation,
README accuracy, architecture diagrams, test coverage, CI presence,
and drift between the spec and the actual code.

Triggered by `harness import` when the imported repo looks "done"
(>=review_pass_threshold features passing, or no features but
substantial code) — or explicitly with --review.
"""

from pathlib import Path
from typing import Optional

from harness.agents.base import BaseAgent
from harness.progress.models import ProjectProgress


_SYSTEM = """You are a senior staff engineer doing a release-readiness review of a project.
Your audit must be concrete, actionable, and skeptical.

Produce a single Markdown document called REVIEW.md with these sections — each
either populated with specific findings or marked "No issues found." Never
omit a section.

## Summary
One paragraph: what the project is and the headline gaps.

## Documentation
- Is the README accurate to the current code?
- Are top-level modules / public APIs documented?
- Stale references, broken links, missing usage examples?

## Architecture & diagrams
- Is there an architecture overview? Is it current?
- Are there sequence/data-flow diagrams where they would help?
- Any drift between the spec (if present) and the actual structure?

## Tests
- Which modules / public functions have no tests?
- Edge cases obviously missing (errors, boundaries, concurrency)?
- Smoke / integration tests vs only unit tests?

## CI / DevOps
- Is there a CI config (GitHub Actions, etc.)? Does it run tests + lint?
- Dockerfile / deployment script / environment docs?
- Lockfile present?

## Spec drift
- For each feature in the spec/feature list: is it actually implemented?
- Anything implemented that is NOT in the spec? (scope creep)

## Recommendations (prioritized)
A short ordered list of concrete next steps. Highest impact first.

Rules:
- Cite specific file paths and line ranges when calling out gaps.
- Do NOT propose massive rewrites. Suggest the smallest meaningful fix.
- If the project is in genuinely good shape, say so — don't invent findings.
"""


class ReviewerAgent(BaseAgent):
    role = "evaluator"  # reuse the evaluator model slot — same kind of task

    def run(self, **kwargs):  # pragma: no cover — interface stub
        raise NotImplementedError("Call review() directly.")

    def review(self, progress: Optional[ProjectProgress] = None) -> str:
        """Audit the project at config.output_dir; write REVIEW.md and return its body."""
        output_dir = Path(self.config.output_dir)
        review_path = output_dir / "REVIEW.md"

        spec_block = ""
        spec_path = self.config.spec_path
        if spec_path.exists():
            spec_text = spec_path.read_text()
            spec_block = f"\n**Project spec ({spec_path.name}):**\n```\n{spec_text}\n```\n"

        feature_block = ""
        if progress and progress.features:
            passing = len(progress.passing_features)
            total = len(progress.features)
            feature_block = (
                f"\n**Feature progress:** {passing}/{total} passing.\n"
                "Feature list (id, name, status):\n"
                + "\n".join(
                    f"- {f.id}: {f.name} [{f.status.value}]" for f in progress.features
                )
                + "\n"
            )

        prompt = (
            f"{_SYSTEM}\n\n"
            f"Project root: {output_dir}\n"
            f"{spec_block}"
            f"{feature_block}\n"
            "Read the codebase, README, docs/, tests/, and any CI config. "
            "Then write the REVIEW.md document to "
            f"`{review_path}`. After writing, print exactly the line "
            "`REVIEW_DONE` and stop. If for some reason you cannot write "
            "files, output the full review wrapped in <review>...</review> tags."
        )

        if self._use_runner:
            output = self._call_via_runner(prompt)
            if review_path.exists():
                return review_path.read_text()
            # Fallback: extract from <review> tag
            import re
            m = re.search(r"<review>\s*(.*?)\s*</review>", output, re.DOTALL)
            if m:
                review_path.write_text(m.group(1).strip())
                return review_path.read_text()
            raise RuntimeError(
                "Reviewer runner produced no REVIEW.md and no <review> block. "
                f"Last 500 chars:\n{output[-500:]}"
            )

        # API mode — single call, write whatever comes back
        text, _ = self._call(
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Project root: {output_dir}\n{spec_block}{feature_block}\n"
                    "Note: in this mode you cannot read files directly. "
                    "Base the review on the spec + feature list above and ask the "
                    "user for any specific files you need quoted. Produce REVIEW.md."
                ),
            }],
            max_tokens=8000,
        )
        review_path.write_text(text)
        return text
