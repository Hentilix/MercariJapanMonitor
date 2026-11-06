# -*- coding: utf-8 -*-
"""Offline tests for the single-instance lock on Windows and POSIX."""

import os
import subprocess
import sys
from pathlib import Path

from app.single_instance import InstanceLock


def test_second_acquire_fails_on_same_path(tmp_path):
    lock_path = tmp_path / "instance.lock"
    first = InstanceLock(lock_path)
    second = InstanceLock(lock_path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()


def test_release_allows_reacquire(tmp_path):
    lock_path = tmp_path / "instance.lock"
    first = InstanceLock(lock_path)
    assert first.acquire() is True
    first.release()

    third = InstanceLock(lock_path)
    assert third.acquire() is True
    third.release()


def test_lock_file_records_pid(tmp_path):
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    assert lock.acquire() is True
    # Windows byte-range locks are mandatory: while held, even reads by other
    # handles are denied, so read after release (content persists on disk).
    lock.release()
    content = lock_path.read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in content


def test_release_is_idempotent(tmp_path):
    lock = InstanceLock(tmp_path / "instance.lock")
    assert lock.acquire() is True
    lock.release()
    lock.release()  # must not raise


def test_default_path_is_in_temp_dir():
    lock = InstanceLock()
    assert lock.path.name == "marketplace_monitor.lock"


def test_lock_excludes_another_process_until_release(tmp_path):
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    contender = """
import sys
from app.single_instance import InstanceLock

lock = InstanceLock(sys.argv[1])
acquired = lock.acquire()
lock.release()
sys.exit(0 if acquired else 1)
"""
    command = [sys.executable, "-c", contender, str(lock_path)]
    project_root = Path(__file__).resolve().parents[1]

    assert lock.acquire() is True
    try:
        result = subprocess.run(
            command, cwd=project_root, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 1, result.stderr
        assert result.stderr == ""
    finally:
        lock.release()

    result = subprocess.run(
        command, cwd=project_root, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_abrupt_process_exit_releases_lock(tmp_path):
    lock_path = tmp_path / "instance.lock"
    owner = """
import os
import sys
from app.single_instance import InstanceLock

lock = InstanceLock(sys.argv[1])
assert lock.acquire()
os._exit(7)  # Skip release(), finally blocks, and interpreter cleanup.
"""
    result = subprocess.run(
        [sys.executable, "-c", owner, str(lock_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 7, result.stderr

    lock = InstanceLock(lock_path)
    try:
        assert lock.acquire() is True
    finally:
        lock.release()
