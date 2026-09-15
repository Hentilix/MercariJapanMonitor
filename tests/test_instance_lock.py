# -*- coding: utf-8 -*-
"""Offline tests for the Phase 5.6 single-instance lock (no real processes)."""

import os

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
