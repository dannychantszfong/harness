"""Claude Agent Harness.

The top-level names are resolved lazily so `import harness.config` (and
every other submodule) does not pull in the orchestrator and all agents.
"""

__all__ = ["HarnessConfig", "Orchestrator"]


def __getattr__(name: str):
    if name == "Orchestrator":
        from harness.orchestrator import Orchestrator

        return Orchestrator
    if name == "HarnessConfig":
        from harness.config import HarnessConfig

        return HarnessConfig
    raise AttributeError(f"module 'harness' has no attribute {name!r}")
