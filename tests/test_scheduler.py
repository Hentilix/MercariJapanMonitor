"""Tests for the APScheduler wiring.

Short intervals only; the scheduled jobs never touch Mercari or SQLite.
"""

import asyncio

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from app.scheduler import ScanScheduler


@pytest.mark.asyncio
async def test_job_fires_periodically():
    state = {"count": 0}

    async def job():
        state["count"] += 1

    scheduler = ScanScheduler(job, IntervalTrigger(seconds=0.1))
    scheduler.start()
    await asyncio.sleep(0.45)
    await scheduler.stop()
    assert state["count"] >= 2


@pytest.mark.asyncio
async def test_runs_do_not_overlap():
    state = {"running": 0, "max_running": 0, "count": 0}

    async def job():
        state["running"] += 1
        state["max_running"] = max(state["max_running"], state["running"])
        state["count"] += 1
        try:
            await asyncio.sleep(0.25)
        finally:
            state["running"] -= 1

    scheduler = ScanScheduler(job, IntervalTrigger(seconds=0.1))
    scheduler.start()
    await asyncio.sleep(0.8)
    await scheduler.stop()
    assert state["max_running"] == 1  # never two instances at once
    assert state["count"] >= 2


@pytest.mark.asyncio
async def test_failed_job_does_not_stop_scheduler():
    state = {"failures": 0, "successes": 0}

    async def job():
        if state["failures"] == 0:
            state["failures"] += 1
            raise RuntimeError("boom")
        state["successes"] += 1

    scheduler = ScanScheduler(job, IntervalTrigger(seconds=0.1))
    scheduler.start()
    await asyncio.sleep(0.45)
    await scheduler.stop()
    assert state["failures"] == 1
    assert state["successes"] >= 1  # next ticks still ran


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_job():
    started = asyncio.Event()
    finished = asyncio.Event()

    async def job():
        started.set()
        await asyncio.sleep(0.25)
        finished.set()

    scheduler = ScanScheduler(job, IntervalTrigger(seconds=0.05))
    scheduler.start()
    for _ in range(100):  # wait until an instance is actually running
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    await scheduler.stop()  # must wait for the in-flight job, not cancel it
    assert finished.is_set()
