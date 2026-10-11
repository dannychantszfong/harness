"""Agent sessions must not outlive Harness.

Harness runs coding agents with full write access to the project. If Harness
dies (terminal closed, `kill`, a scheduler stopping the job) while an agent
keeps running, that agent goes on editing the tree, and the next
`harness resume` starts a second one on top of it.

- POSIX: SIGTERM and SIGHUP raise SystemExit, so the code that started the
  session (runners.base.run_process) kills its process group on the way
  out, exactly as it does for Ctrl+C. On Linux every session also gets
  PR_SET_PDEATHSIG=SIGKILL (runners.base.child_popen_kwargs), which covers
  a Harness killed with SIGKILL.
- Windows: Harness puts itself in a Job Object with KILL_ON_JOB_CLOSE. Every
  process it starts joins the job when it is created; when Harness exits,
  for any reason, the last handle to the job closes and Windows kills the
  job's remaining processes.

install() is called by the CLI commands that run agents, never at import.
"""

from __future__ import annotations

import os
import signal
import threading

_installed = False
_job_handle = None  # kept open for the life of the process (Windows)


def install() -> None:
    """Idempotent. Safe to call from any command that may start agents."""
    global _installed
    if _installed:
        return
    _installed = True
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
            sig = getattr(signal, name, None)
            # Leave handlers someone else chose alone — including SIG_IGN
            # (`nohup harness …` must keep ignoring SIGHUP).
            if sig is not None and signal.getsignal(sig) == signal.SIG_DFL:
                signal.signal(sig, _exit_on_signal)
    if os.name == "nt":
        _join_kill_on_close_job()


def _exit_on_signal(signum, frame) -> None:  # noqa: ARG001 — signal handler signature
    # A second signal while shutting down terminates immediately.
    signal.signal(signum, signal.SIG_DFL)
    raise SystemExit(128 + signum)


def _join_kill_on_close_job() -> bool:
    """Put this process in a kill-on-close Job Object. False if not possible
    (e.g. an enclosing job forbids it); Harness then relies on taskkill in
    the exception paths, as before."""
    global _job_handle
    if _job_handle is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job_object_extended_limit_information = 9
        job_object_limit_kill_on_job_close = 0x2000

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
        ok = kernel32.SetInformationJobObject(
            job, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info),
        ) and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
        if not ok:
            kernel32.CloseHandle(job)
            return False
        _job_handle = job
        return True
    except (OSError, AttributeError, ValueError):
        return False


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid is running (never signals it)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) would *terminate* the process on Windows.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
