"""Bounded, provider-neutral subprocess execution for critic protocols."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
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
            )
            stop_monitor = threading.Event()
            limit_exceeded = threading.Event()

            def monitor_output() -> None:
                while not stop_monitor.wait(0.02):
                    total = (
                        os.fstat(stdout_handle.fileno()).st_size
                        + os.fstat(stderr_handle.fileno()).st_size
                    )
                    if total > max_output_bytes:
                        limit_exceeded.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
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
                process.kill()
                process.communicate()
            except KeyboardInterrupt:
                process.kill()
                process.communicate()
                raise
            finally:
                stop_monitor.set()
                monitor.join()

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
