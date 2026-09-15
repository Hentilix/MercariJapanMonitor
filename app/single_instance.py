# -*- coding: utf-8 -*-
"""Single-instance guard (Phase 5.6).

Ensures only one MarketplaceMonitor process runs at a time. A second start
exits *before* creating Database / MercariClient / scheduler / HTTP server,
so there can never be duplicate scheduler jobs, HTTP port conflicts or
duplicate emails.

Implementation: an exclusive byte-range lock on a lock file in the user's
temp dir, using msvcrt.locking (a real OS lock, not PID-file guessing).
The file content (PID + start time) is informational only — the OS lock is
the source of truth. When the owning process exits, even on a crash, Windows
releases the lock automatically, so a stale lock file never blocks startup.
"""

import logging
import msvcrt
import os
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_LOCK_NAME = "marketplace_monitor.lock"


class InstanceLock:
    """Exclusive single-instance lock backed by a locked file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = (
            Path(path)
            if path is not None
            else Path(tempfile.gettempdir()) / DEFAULT_LOCK_NAME
        )
        self._file = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """Try to take the lock. True = acquired; False = already running."""
        if self._file is not None:
            return True
        try:
            f = open(self._path, "a+", encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot open lock file %s: %s", self._path, exc)
            return False
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            # Lock held by another live process.
            f.close()
            return False
        try:
            f.seek(0)
            f.truncate()
            f.write(
                "pid=%d started=%s\n"
                % (os.getpid(), datetime.now().isoformat(timespec="seconds"))
            )
            f.flush()
        except OSError:
            pass  # informational only; the lock itself is what matters
        self._file = f
        return True

    def release(self) -> None:
        """Release the lock. Safe to call multiple times."""
        if self._file is None:
            return
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._file.close()
        self._file = None
