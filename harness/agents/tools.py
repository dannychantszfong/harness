"""Project-inspection tools for api-mode agents (ADR-016, decision D-04).

In orchestration_mode="api" the evaluator is a Messages API call, not a
coding agent, so it cannot open files or run the app by itself. These tools
give it a bounded, read-only view of the project:

  read_file   — read a text file under the project root
  list_files  — list a directory (skipping .git, node_modules, …)
  search      — regex search across project files
  run_command — run an allow-listed command: the startup command, the
                project's test command, `git diff …`, `git log …`

The allow-list is the permission boundary. Which tools to call, and in what
order, is the model's own judgement — nothing here scripts an inspection.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from harness.runners.base import child_popen_kwargs, kill_process_tree

_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".mypy_cache", "target", ".next", ".turbo",
})
_MAX_READ_CHARS = 60_000
_MAX_OUTPUT_CHARS = 20_000
_MAX_LIST_ENTRIES = 400
_MAX_SEARCH_HITS = 200
_MAX_SEARCH_FILE_BYTES = 1_000_000
_GIT_SUBCOMMANDS = ("diff", "log", "status", "show")
# git options that write files, read outside the repository, or run
# programs — refused whatever the subcommand (prefix match). `-O<file>`
# (diff order file) reads an arbitrary path.
_GIT_FORBIDDEN_LONG_OPTIONS = (
    "--output", "--no-index", "--ext-diff", "--textconv", "--exec",
    "--git-dir", "--work-tree", "--upload-pack", "--config",
)
# Repo config could name an external diff or textconv program.
_GIT_SAFETY_ARGS = {"diff": ["--no-ext-diff", "--no-textconv"],
                    "log": ["--no-ext-diff", "--no-textconv"],
                    "show": ["--no-ext-diff", "--no-textconv"]}
# Shell metacharacters are refused outright: commands run without a shell,
# so they could only be an attempt to smuggle a second command.
_SHELL_META = re.compile(r"[;&|<>`$\n\r]")

INSPECTION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file in the project. Paths are relative to the project root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "First line to return (1-based)", "minimum": 1},
                "limit": {"type": "integer", "description": "Maximum number of lines", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories under a project path (dependency and VCS dirs are skipped).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 2},
            },
        },
    },
    {
        "name": "search",
        "description": "Search project files for a regular expression; returns path:line: text matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string", "description": "Only files matching this glob, e.g. *.py"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run one allow-listed command in the project root and return its exit code and "
            "output. Allowed: the startup command, the project's test command, and "
            "`git diff|log|status|show …`. Long-running commands are stopped at the timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


class ProjectTools:
    """Executes INSPECTION_TOOLS calls against one project directory."""

    def __init__(self, config, root: Path | None = None) -> None:
        self.config = config
        self.root = Path(root or config.output_dir).resolve()
        self.command_timeout = int(getattr(config, "api_command_timeout_seconds", 120) or 120)

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return INSPECTION_TOOLS

    def allowed_commands(self) -> list[str]:
        startup = getattr(self.config, "startup_command_for_platform", None)
        test = getattr(self.config, "test_command", None)
        return [c.strip() for c in (startup, test) if c and c.strip()]

    def execute(self, name: str, tool_input: dict[str, Any] | None) -> str:
        """Run one tool call. Never raises: every problem is the tool's answer,
        so a malformed call from the model cannot end the run."""
        if not isinstance(tool_input, dict):
            tool_input = {}
        try:
            if name == "read_file":
                return self.read_file(tool_input.get("path", ""), tool_input.get("offset"), tool_input.get("limit"))
            if name == "list_files":
                return self.list_files(tool_input.get("path", "."), tool_input.get("max_depth", 2))
            if name == "search":
                return self.search(tool_input.get("pattern", ""), tool_input.get("path", "."), tool_input.get("glob"))
            if name == "run_command":
                return self.run_command(tool_input.get("command", ""))
        except ToolError as exc:
            return f"ERROR: {exc}"
        except (ValueError, TypeError, OSError, RecursionError) as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        return f"ERROR: unknown tool {name!r}"

    # ── tools ───────────────────────────────────────────────────────────

    def read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if offset or limit:
            lines = text.splitlines()
            start = max(int(offset or 1), 1) - 1
            end = start + int(limit) if limit else len(lines)
            text = "\n".join(lines[start:end])
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + f"\n…(truncated at {_MAX_READ_CHARS} characters)"
        return text

    def list_files(self, path: str = ".", max_depth: int = 2) -> str:
        base = self._resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        max_depth = max(1, min(int(max_depth or 2), 6))
        entries: list[str] = []
        for current, dirs, files in os.walk(base):
            rel = Path(current).relative_to(base)
            depth = len(rel.parts)
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            if depth >= max_depth:
                dirs[:] = []
            for d in dirs:
                entries.append((rel / d).as_posix() + "/")
            for f in sorted(files):
                entries.append((rel / f).as_posix())
            if len(entries) >= _MAX_LIST_ENTRIES:
                entries = entries[:_MAX_LIST_ENTRIES] + ["…(listing truncated)"]
                break
        return "\n".join(entries) or "(empty)"

    def search(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        if not pattern:
            raise ToolError("pattern is required")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        base = self._resolve(path)
        files = [base] if base.is_file() else self._walk(base)
        hits: list[str] = []
        for file in files:
            if glob and not file.match(glob):
                continue
            if not self._inside(file):
                continue  # a symlink pointing out of the project
            try:
                if file.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                    continue
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # binary or unreadable
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{file.relative_to(self.root).as_posix()}:{n}: {line.strip()[:200]}")
                    if len(hits) >= _MAX_SEARCH_HITS:
                        return "\n".join(hits) + "\n…(more matches not shown)"
        return "\n".join(hits) or "(no matches)"

    def run_command(self, command: str) -> str:
        command = (command or "").strip()
        argv = self._allowed_argv(command)  # raises if not allow-listed
        if argv is not None:
            command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        return _run_bounded(command, self.root, self.command_timeout)

    # ── helpers ─────────────────────────────────────────────────────────

    def _allowed_argv(self, command: str) -> list[str] | None:
        """None for an exact allow-listed command line; the (hardened) argv
        for a git inspection command; ToolError for anything else."""
        if not command:
            raise ToolError("command is required")
        allowed = self.allowed_commands()
        if command not in allowed:
            if _SHELL_META.search(command):
                raise ToolError("shell operators are not allowed; run one command at a time")
            argv = _split(command)
            if not (len(argv) >= 2 and argv[0] == "git" and argv[1] in _GIT_SUBCOMMANDS):
                listed = ", ".join(f"`{c}`" for c in allowed) or "(none configured)"
                raise ToolError(
                    f"command not allowed. Allowed: {listed}, "
                    f"and git {'/'.join(_GIT_SUBCOMMANDS)} with arguments"
                )
            for arg in argv[2:]:
                bare = arg.strip("\"'")
                if _forbidden_git_option(bare):
                    raise ToolError(f"git option {bare.split('=')[0]} is not allowed")
            return [argv[0], argv[1], *_GIT_SAFETY_ARGS.get(argv[1], []), *argv[2:]]
        return None  # an exact allow-listed command line, run as written

    def _resolve(self, path: str) -> Path:
        candidate = (self.root / (path or ".")).resolve()
        if not self._inside(candidate):
            raise ToolError(f"path is outside the project: {path}")
        return candidate

    def _inside(self, path: Path) -> bool:
        """Whether path, with symlinks resolved, is within the project."""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        return resolved == self.root or self.root in resolved.parents

    def _walk(self, base: Path):
        # os.walk does not descend into symlinked directories; files that are
        # symlinks are yielded and checked by the caller (_inside).
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in files:
                yield Path(current) / f


class ToolError(Exception):
    pass


# Real git options that happen to be prefixes of forbidden ones.
_GIT_EXACT_SAFE_LONG_OPTIONS = frozenset({"--text"})


def _forbidden_git_option(arg: str) -> bool:
    """True for git options that write files, read outside the repo or run
    programs — including the forms git also accepts:
    - short-option bundles: `-pO<file>` is `-p -O<file>`;
    - unambiguous abbreviations of long options: `--outp=<file>`, `--textc`.
    """
    if arg == "--" or not arg.startswith("-"):
        return False
    if not arg.startswith("--"):
        return "O" in arg[1:]
    name = arg.split("=", 1)[0]
    if name in _GIT_EXACT_SAFE_LONG_OPTIONS:
        return False
    return any(
        name.startswith(forbidden) or (len(name) > 3 and forbidden.startswith(name))
        for forbidden in _GIT_FORBIDDEN_LONG_OPTIONS
    )


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ToolError(f"could not parse command: {exc}") from exc


def _popen_args(command: str):
    """What to hand Popen for a command line, without a shell.

    POSIX: an argv from shlex. Windows: the command line itself (programs
    parse their own arguments there, and shlex's non-POSIX mode keeps
    quotes), with only the executable resolved — `npm`, `npx`, … are .cmd
    shims CreateProcess cannot find by bare name.
    """
    if os.name != "nt":
        argv = _split(command)
        return [shutil.which(argv[0]) or argv[0], *argv[1:]]
    command = command.strip()
    first = _split(command)[0]
    exe = first.strip('"')
    resolved = shutil.which(exe) or exe
    return subprocess.list2cmdline([resolved]) + command[len(first):]


def _run_bounded(command: str, cwd: Path, timeout: int) -> str:
    """Run without a shell; kill the whole process tree on timeout.

    Output goes to a temp file rather than a pipe: a dev server started by
    the startup script would otherwise hold the pipe open after the kill.
    """
    popen_kwargs = child_popen_kwargs()  # own process group: killpg on timeout
    args = _popen_args(command)
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(args, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **popen_kwargs)
        except OSError as exc:
            return f"ERROR: could not start `{command}`: {exc}"
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
        out.seek(0)
        output = out.read().decode("utf-8", errors="replace")
    if len(output) > _MAX_OUTPUT_CHARS:
        output = "…(earlier output truncated)\n" + output[-_MAX_OUTPUT_CHARS:]
    status = (
        f"timed out after {timeout}s and was stopped (long-running server?)"
        if timed_out else f"exit code {proc.returncode}"
    )
    return f"{status}\n{output}"
