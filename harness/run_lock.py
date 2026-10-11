"""One Harness run per project at a time.

Two instances in one project directory (a manual `harness resume` plus the
scheduled auto-resume job, or two terminals) would run two generators, write
features.json from two processes, and `commit_all` / `reset --hard` from one
would race the other's agent session.

The lock is an OS file lock on <project>/.harness/run.lock (flock on POSIX,
a byte-range lock on Windows). The operating system releases it when the
holding process exits, however it exits, so a crashed or killed run never
leaves a stale lock behind. The file's content (pid, host, start time,
command) is only there to tell the user who holds it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from harness.timeutil import utcnow

LOCK_RELPATH = ".harness/run.lock"
# Windows locks a byte range; lock one far past the content so readers can
# still read who holds it.
_WINDOWS_LOCK_OFFSET = 1 << 28
# How long to wait for the OS to drop the lock of a holder that has exited.
_DEAD_HOLDER_GRACE_SECONDS = 5.0


class ProjectLockedError(RuntimeError):
    """Another live Harness process holds this project's run lock."""

    def __init__(self, path: Path, holder: Optional[dict]):
        self.path = path
        self.holder = holder or {}
        who = []
        if self.holder.get("command"):
            who.append(f"`{self.holder['command']}`")
        if self.holder.get("pid"):
            who.append(f"pid {self.holder['pid']}")
        if self.holder.get("host"):
            who.append(f"on {self.holder['host']}")
        if self.holder.get("started_at"):
            who.append(f"started {self.holder['started_at']}")
        detail = f" ({', '.join(who)})" if who else ""
        super().__init__(
            f"Another Harness run is already working on {path.parent.parent}{detail}.\n"
            "Wait for it to finish or stop it, then try again. The lock is released "
            f"automatically when that process exits ({path})."
        )


@dataclass
class _Held:
    fd: int
    path: Path


_held: dict[str, _Held] = {}


def lock_path(project_dir: Path) -> Path:
    return Path(project_dir) / LOCK_RELPATH


def is_held_here(project_dir: Path) -> bool:
    return _key(project_dir) in _held


def claim(project_dir: Path, command: str = "harness") -> Path:
    """Take the project's run lock for the rest of this process's life.

    No-op if this process already holds it. Raises ProjectLockedError if
    another process does.
    """
    key = _key(project_dir)
    if key in _held:
        return _held[key].path
    held = _acquire(Path(key), command)
    _held[key] = held
    return held.path


def release(project_dir: Path) -> None:
    held = _held.pop(_key(project_dir), None)
    if held is None:
        return
    try:
        os.ftruncate(held.fd, 0)
    except OSError:
        pass
    try:
        _unlock(held.fd)
    except OSError:
        pass
    os.close(held.fd)


@contextmanager
def hold(project_dir: Path, command: str = "harness") -> Iterator[Path]:
    """claim() for a block; released afterwards only if this block took it."""
    already = is_held_here(project_dir)
    path = claim(project_dir, command)
    try:
        yield path
    finally:
        if not already:
            release(project_dir)


def _key(project_dir: Path) -> str:
    return str(Path(project_dir).expanduser().resolve())


def _acquire(project_dir: Path, command: str) -> _Held:
    path = lock_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _DEAD_HOLDER_GRACE_SECONDS
    replaced = 0
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_lock(fd):
            holder = _read_holder(path)
            os.close(fd)
            if _holder_is_dead(holder) and time.monotonic() < deadline:
                # The holder has exited; Windows releases a dead process's
                # locks asynchronously. Give it a moment.
                time.sleep(0.2)
                continue
            raise ProjectLockedError(path, holder)
        if not _still_at_path(fd, path):
            # Replaced between open() and the lock; lock the new file.
            _unlock(fd)
            os.close(fd)
            replaced += 1
            if replaced > 3:
                raise ProjectLockedError(path, _read_holder(path))
            continue
        _write_holder(fd, command)
        return _Held(fd=fd, path=path)


def _holder_is_dead(holder: Optional[dict]) -> bool:
    """True only when the recorded holder is a process on this host that has
    exited. Unknown holders count as alive."""
    if not holder or holder.get("host") != socket.gethostname():
        return False
    try:
        pid = int(holder.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return False
    from harness.process_guard import pid_alive

    return pid > 0 and not pid_alive(pid)


def _write_holder(fd: int, command: str) -> None:
    info = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "command": command,
    }
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, (json.dumps(info) + "\n").encode("utf-8"))


def _read_holder(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _still_at_path(fd: int, path: Path) -> bool:
        return True  # an open file cannot be replaced on Windows

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _still_at_path(fd: int, path: Path) -> bool:
        try:
            return os.fstat(fd).st_ino == os.stat(path).st_ino
        except OSError:
            return False
