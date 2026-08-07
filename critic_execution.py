"""Bounded, provider-neutral subprocess execution for critic protocols."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ExecutorResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limit_exceeded: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    """Place a process in a kill-on-close Windows Job Object when available."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(int(process._handle))
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume a process created with CREATE_SUSPENDED after job assignment."""
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(wintypes.HANDLE(int(process._handle)))
    if status != 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")


def _read_capped_outputs(
    stdout_handle: BinaryIO,
    stderr_handle: BinaryIO,
    max_output_bytes: int,
) -> tuple[bytes, bytes, bool, bool]:
    """Read a combined bounded prefix, prioritizing the report on stdout."""
    stdout_handle.flush()
    stderr_handle.flush()
    stdout_size = os.fstat(stdout_handle.fileno()).st_size
    stderr_size = os.fstat(stderr_handle.fileno()).st_size

    stdout_handle.seek(0)
    stdout = stdout_handle.read(max_output_bytes)
    remaining = max_output_bytes - len(stdout)
    stderr_handle.seek(0)
    stderr = stderr_handle.read(remaining)
    return (
        stdout,
        stderr,
        len(stdout) < stdout_size,
        len(stderr) < stderr_size,
    )


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of the executor and descendants it created."""
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.2)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def execute_with_limits(
    executor: list[str],
    prompt: bytes,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    capture_dir: Path,
) -> ExecutorResult:
    """Execute with bounded memory and terminate once combined output is too large."""
    if max_output_bytes <= 0:
        raise ValueError("max output bytes must be a positive integer")

    with tempfile.TemporaryDirectory(prefix=".capture-", dir=capture_dir) as temp_dir:
        temp_path = Path(temp_dir)
        if os.name == "posix":
            os.chmod(temp_path, 0o700)
        with (temp_path / "stdout").open("w+b") as stdout_handle, (
            temp_path / "stderr"
        ).open("w+b") as stderr_handle:
            if os.name == "posix":
                os.chmod(temp_path / "stdout", 0o600)
                os.chmod(temp_path / "stderr", 0o600)

            process = subprocess.Popen(
                executor,
                stdin=subprocess.PIPE,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=os.name == "posix",
                creationflags=(
                    (
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                    )
                    if os.name == "nt"
                    else 0
                ),
            )
            windows_job = _create_windows_kill_job(process)
            if os.name == "nt":
                try:
                    _resume_windows_process(process)
                except OSError:
                    if windows_job is not None:
                        _close_windows_handle(windows_job)
                    else:
                        process.kill()
                    process.wait()
                    raise
            stop_monitor = threading.Event()
            limit_exceeded = threading.Event()
            termination_lock = threading.Lock()

            def terminate() -> None:
                nonlocal windows_job
                with termination_lock:
                    if windows_job is not None:
                        _close_windows_handle(windows_job)
                        windows_job = None
                    else:
                        _terminate_process_tree(process)

            def monitor_output() -> None:
                while not stop_monitor.wait(0.02):
                    total = (
                        os.fstat(stdout_handle.fileno()).st_size
                        + os.fstat(stderr_handle.fileno()).st_size
                    )
                    if total > max_output_bytes:
                        limit_exceeded.set()
                        terminate()
                        return

            monitor = threading.Thread(
                target=monitor_output,
                name="critic-output-monitor",
                daemon=True,
            )
            monitor.start()
            timed_out = False
            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate()
                process.communicate()
            except KeyboardInterrupt:
                terminate()
                process.communicate()
                raise
            finally:
                stop_monitor.set()
                monitor.join()
                if windows_job is not None:
                    _close_windows_handle(windows_job)
                    windows_job = None
                    time.sleep(0.1)

            total_size = (
                os.fstat(stdout_handle.fileno()).st_size
                + os.fstat(stderr_handle.fileno()).st_size
            )
            if total_size > max_output_bytes:
                limit_exceeded.set()
            stdout, stderr, stdout_truncated, stderr_truncated = _read_capped_outputs(
                stdout_handle,
                stderr_handle,
                max_output_bytes,
            )
            return ExecutorResult(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                output_limit_exceeded=limit_exceeded.is_set(),
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
