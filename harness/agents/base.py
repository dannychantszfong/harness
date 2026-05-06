"""Base agent with streaming, prompt caching, and token tracking."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional
import anthropic
from rich.console import Console

from harness.config import HarnessConfig

if TYPE_CHECKING:
    from harness.runners.base import CodeRunner

console = Console()


class TokenUsage:
    def __init__(self) -> None:
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_read_tokens: int = 0
        self.cache_write_tokens: int = 0

    def update(self, usage: Any) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0)
        self.output_tokens += getattr(usage, "output_tokens", 0)
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __repr__(self) -> str:
        return (
            f"TokenUsage(in={self.input_tokens}, out={self.output_tokens}, "
            f"cache_read={self.cache_read_tokens}, cache_write={self.cache_write_tokens})"
        )


class BaseAgent(ABC):
    """Common interface for all harness agents.

    Each agent owns one conceptual responsibility (plan / generate / evaluate).
    Agents are stateless across calls; state lives in files on disk.
    """

    role: str = "agent"

    def __init__(
        self,
        config: HarnessConfig,
        client: Optional[anthropic.Anthropic] = None,
        runner: Optional["CodeRunner"] = None,
    ) -> None:
        self.config = config
        self.runner = runner
        # Only instantiate the Anthropic client when API orchestration is needed
        if config.orchestration_mode == "api" or client is not None:
            self.client = client or anthropic.Anthropic()
        else:
            self.client = None  # type: ignore[assignment]
        self.usage = TokenUsage()

    @property
    def model(self) -> str:
        return getattr(self.config, f"{self.role}_model", self.config.planner_model)

    # ------------------------------------------------------------------
    # Core LLM call helpers
    # ------------------------------------------------------------------

    def _call(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
        stream: bool = True,
        extra_headers: dict | None = None,
    ) -> tuple[str, list[dict]]:
        """Make a single model call. Returns (text, tool_use_blocks)."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Cache the system prompt — saves tokens on repeated calls in tight loops
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        if tools:
            params["tools"] = tools
        if extra_headers:
            params["extra_headers"] = extra_headers

        full_text = ""
        tool_uses: list[dict] = []

        console.print(f"\n[bold cyan]── {self.role.upper()} ──[/bold cyan]")

        if stream and not tools:
            with self.client.messages.stream(**params) as s:
                for chunk in s.text_stream:
                    print(chunk, end="", flush=True)
                    full_text += chunk
            final = s.get_final_message()
            self.usage.update(final.usage)
            print()
        else:
            resp = self.client.messages.create(**params)
            self.usage.update(resp.usage)
            for block in resp.content:
                if block.type == "text":
                    full_text = block.text
                    console.print(block.text)
                elif block.type == "tool_use":
                    tool_uses.append({"id": block.id, "name": block.name, "input": block.input})

        return full_text, tool_uses

    def _call_via_runner(self, prompt: str) -> str:
        """Send a plain-text prompt through the runner instead of the Anthropic API.

        Used in orchestration_mode="runner" so that planner/evaluator run on the
        same subscription runner as the generator (no ANTHROPIC_API_KEY needed).
        """
        if self.runner is None:
            raise RuntimeError(
                "orchestration_mode='runner' but no runner was passed to this agent."
            )
        cwd = str(self.config.output_path)
        result = self.runner.implement(prompt, cwd=cwd)
        if not result.success:
            if result.rate_limit_reset_at is not None:
                from harness.runners.base import RunnerRateLimitedError
                raise RunnerRateLimitedError(
                    reset_at=result.rate_limit_reset_at,
                    raw_message=result.error or "",
                )
            raise RuntimeError(f"Runner failed during orchestration: {result.error}")
        # Accumulate token data when the runner provides it
        if result.input_tokens:
            self.usage.input_tokens += result.input_tokens
        if result.output_tokens:
            self.usage.output_tokens += result.output_tokens
        return result.output

    @property
    def _use_runner(self) -> bool:
        return self.config.orchestration_mode == "runner"

    @abstractmethod
    def run(self, **kwargs) -> Any:
        """Each agent implements its primary action here."""
        ...
