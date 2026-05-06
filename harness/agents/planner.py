"""Planner agent — expands a brief into a comprehensive product specification.

From article 1: the planner emphasizes scope and high-level design while
deliberately avoiding premature technical details.  It runs at the start of
each major session to refresh context about goals and boundaries.
"""

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from harness.agents.base import BaseAgent

console = Console()

_SYSTEM = """You are a senior product architect.
Your role is to expand a short project brief into a comprehensive product specification.

Guidelines:
- Focus on WHAT the product does, not HOW it is built.
- Define clear scope boundaries (what's in, what's out).
- Describe user-facing features and flows, not implementation details.
- Include success criteria that the QA evaluator can verify.
- Be exhaustive — missing a feature here means it won't be built.
- Write in structured markdown with clear sections.

Do NOT write code. Do NOT specify technical architecture.
"""

_REFINE_INSTRUCTION = (
    "The user has reviewed the spec and provided the following feedback. "
    "Update the spec to address their concerns. Output the complete revised spec."
)


class PlannerAgent(BaseAgent):
    role = "planner"

    def run(self, brief: str) -> str:
        """Expand a brief into a full product spec. Returns the spec as a string."""
        prompt = (
            f"{_SYSTEM}\n\n"
            f"Project brief:\n{brief}\n\n"
            "Please produce a comprehensive product specification. "
            "Be thorough — this spec drives the entire implementation."
        )

        if self._use_runner:
            return self._call_via_runner(prompt)

        messages = [{"role": "user", "content": prompt}]
        spec, _ = self._call(system=_SYSTEM, messages=messages, max_tokens=16_000)
        return spec

    def align_requirements(self, brief: str) -> str:
        """Interactive multi-turn loop: planner proposes a spec, user refines it.

        Keeps the full conversation history so each refinement has context.
        Returns the final confirmed spec.
        """
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Project brief:\n{brief}\n\n"
                    "Please produce a comprehensive product specification. "
                    "Be thorough — this spec drives the entire implementation."
                ),
            }
        ]

        console.print("\n[bold blue]── Requirement Alignment ──[/bold blue]")
        console.print("[dim]The planner will draft a spec. Review it and give feedback, "
                      "or press Enter to confirm.[/dim]\n")

        iteration = 0
        while True:
            iteration += 1

            if self._use_runner:
                # Build a single prompt that includes the full conversation so far
                history = "\n\n".join(
                    f"[{m['role'].upper()}]\n{m['content']}" for m in messages
                )
                spec = self._call_via_runner(f"{_SYSTEM}\n\n{history}")
            else:
                spec, _ = self._call(
                    system=_SYSTEM,
                    messages=messages,
                    max_tokens=16_000,
                )

            # Append the assistant's response to keep conversation history
            messages.append({"role": "assistant", "content": spec})

            # Show the spec in a nice panel
            console.print()
            console.print(Panel(
                Markdown(spec),
                title=f"[bold cyan]Draft Spec (round {iteration})[/bold cyan]",
                border_style="cyan",
            ))
            console.print()

            # Ask for user feedback
            feedback = console.input(
                "[bold]Feedback[/bold] [dim](press Enter to confirm, or describe what to change)[/dim]: "
            ).strip()

            if not feedback:
                console.print("[green]✓ Requirements confirmed.[/green]\n")
                return spec

            # Feed the user's feedback back into the conversation
            messages.append({
                "role": "user",
                "content": f"{_REFINE_INSTRUCTION}\n\nFeedback: {feedback}",
            })
