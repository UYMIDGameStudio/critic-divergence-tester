"""Cross-process serialization for mutations of one local project directory."""

from __future__ import annotations

import os
import errno
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator
from weakref import WeakValueDictionary


class ProjectMutationLockedError(RuntimeError):
    """Raised when another process already owns a project's mutation lock."""


_THREAD_LOCKS: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCAL = threading.local()


def _thread_state() -> dict[str, tuple[int, BinaryIO]]:
    state = getattr(_LOCAL, "project_locks", None)
    if state is None:
        state = {}
        _LOCAL.project_locks = state
    return state


def _lock_byte(handle: BinaryIO) -> None:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise
        raise ProjectMutationLockedError(
            "project is already being modified by another local process"
        ) from exc


def _unlock_byte(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def project_mutation_lock(project_dir: Path | str) -> Iterator[None]:
    """Acquire a non-blocking, re-entrant lock for one existing directory.

    Threads in this process are serialized with an ``RLock``. The first entry
    by a thread also locks one byte of ``.mutation.lock`` so a competing CLI or
    local UI process fails immediately instead of silently racing or waiting.
    """

    candidate = Path(project_dir)
    if candidate.is_symlink():
        raise ValueError("project directory must not be a symbolic link")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("project mutation lock requires an existing directory")
    key = os.path.normcase(str(root))
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())

    with thread_lock:
        state = _thread_state()
        current = state.get(key)
        if current is not None:
            depth, handle = current
            state[key] = (depth + 1, handle)
            try:
                yield
            finally:
                depth, handle = state[key]
                state[key] = (depth - 1, handle)
            return

        lock_path = root / ".mutation.lock"
        if lock_path.is_symlink():
            raise ValueError("project lock file must not be a symbolic link")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("project lock file must be regular and have a single link")
            # Acquire before initializing the byte: two first-time writers must
            # never write into each other's locked region (notably on Windows).
            _lock_byte(handle)
            state[key] = (1, handle)
            try:
                if info.st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                yield
            finally:
                state.pop(key, None)
                _unlock_byte(handle)
