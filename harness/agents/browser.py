"""Experimental browser verification for the evaluator (C-07).

When `evaluator_browser_verification` is on, runner-mode evaluator sessions
on Claude Code (subprocess / sdk runners) get a Playwright MCP server via
`--mcp-config`, the `mcp__playwright` tools on their allow-list, and an
instruction to exercise the acceptance criteria in the running app.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from harness.progress.tracker import write_atomic

DEFAULT_MCP_CONFIG_RELPATH = ".harness/playwright-mcp.json"
PLAYWRIGHT_SERVER = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
# Claude Code permission rule covering every tool of the "playwright" server.
PLAYWRIGHT_TOOL_RULE = "mcp__playwright"
BROWSER_RUNNERS = frozenset({"subprocess", "sdk"})


def browser_supported(runner) -> bool:
    runtime = getattr(getattr(runner, "runner_type", None), "value", None)
    return runtime in BROWSER_RUNNERS


def mcp_config_path(config) -> Path:
    """The MCP config the evaluator uses; writes the default one if needed."""
    project_dir = Path(config.output_dir)
    if config.evaluator_mcp_config:
        path = Path(config.evaluator_mcp_config).expanduser()
        return path if path.is_absolute() else (project_dir / path)
    path = project_dir / DEFAULT_MCP_CONFIG_RELPATH
    if not path.exists():
        payload = {"mcpServers": {"playwright": PLAYWRIGHT_SERVER}}
        write_atomic(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    return path


def load_mcp_servers(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError(f"{path} has no mcpServers entries")
    return servers


def browser_preflight_error(config) -> str | None:
    """None if every configured MCP server command is launchable, else why not."""
    try:
        path = mcp_config_path(config)
        servers = load_mcp_servers(path)
    except (OSError, ValueError) as exc:
        return f"Browser verification is enabled but the MCP config is unusable: {exc}"
    for name, server in servers.items():
        command = server.get("command") if isinstance(server, dict) else None
        if not command:
            continue  # url-based servers need no local binary
        if shutil.which(command) is None:
            hint = " Install Node.js (it provides npx)." if command in ("npx", "node") else ""
            return (
                f"Browser verification is enabled but MCP server {name!r} cannot start: "
                f"`{command}` is not on PATH.{hint} Or set "
                "evaluator_browser_verification: false."
            )
    return None
