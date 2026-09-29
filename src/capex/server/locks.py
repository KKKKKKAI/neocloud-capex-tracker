"""Advisory file locks under run/, shared by the scheduler, its jobs and
manual CLI runs.

    with file_lock(PIPELINE, wait_s=60):
        run_watcher(...)

`pipeline` serialises everything that writes filings, extractions or the
generated site (watcher jobs, regenerate, publish, `capex monitor`);
`scheduler` keeps a second scheduler from starting. Locks are POSIX
flock()s, released by the kernel when the holder exits, so a crash never
leaves one behind. On Windows (development only) they are no-ops.
"""
from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator

from .. import paths

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

PIPELINE = "pipeline"
SCHEDULER = "scheduler"


class LockBusyError(RuntimeError):
    """Another process holds the lock."""


@contextlib.contextmanager
def file_lock(name: str, *, wait_s: float = 0.0) -> Iterator[None]:
    """Hold run/<name>.lock exclusively; LockBusyError after `wait_s` seconds."""
    path = paths.run_dir() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="ascii") as handle:
        if fcntl is not None:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockBusyError(f"{name} lock is held by another process") from None
                    time.sleep(0.5)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield


def is_locked(name: str) -> bool:
    """True while another process holds `name`."""
    try:
        with file_lock(name):
            return False
    except LockBusyError:
        return True
