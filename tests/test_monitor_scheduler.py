"""Offline tests for MonitorScheduler (Phase 5.3-B).

Real SQLite (tmp_path) provides Monitor rows; the scan function is a fake
that never touches Mercari / DeepSeek / SMTP.
"""

import asyncio
from datetime import timedelta

import pytest

from app.database import Database
from app.scheduler import MonitorScheduler


def make_monitor_fields(name, interval_minutes=30, enabled=True):
    return dict(
        name=name,
        keywords=f"kw {name}",
        keyword_mode="AND",
        min_price=None,
        max_price=None,
        filter_words="",
        ai_requirement="",
        interval_minutes=interval_minutes,
        notification_email=None,
        enabled=enabled,
    )


def make_db(tmp_path, *field_dicts):
    db = Database(tmp_path / "test.db")
    monitors = [db.get_monitor(db.create_monitor(**f)) for f in field_dicts]
    return db, monitors


def make_scan(sleep=0.0, fail_ids=frozenset()):
    state = {
        "runs": [],           # monitor ids in start order
        "active": set(),      # currently running monitor ids
        "peak_active": 0,     # max simultaneous scans (across monitors)
        "started": {},        # id -> asyncio.Event set at scan start
        "finished": {},       # id -> asyncio.Event set at scan end
    }

    async def scan(monitor_id):
        state["runs"].append(monitor_id)
        state["active"].add(monitor_id)
        state["peak_active"] = max(state["peak_active"], len(state["active"]))
        started = state["started"].get(monitor_id)
        if started is not None:
            started.set()
        try:
            if sleep:
                await asyncio.sleep(sleep)
            if monitor_id in fail_ids:
                raise RuntimeError(f"boom-{monitor_id}")
        finally:
            state["active"].discard(monitor_id)
            finished = state["finished"].get(monitor_id)
            if finished is not None:
                finished.set()

    return scan, state


def register_event(state, monitor_id):
    event = asyncio.Event()
    state["finished"][monitor_id] = event
    return event


async def wait_event(event, timeout=5.0):
    await asyncio.wait_for(event.wait(), timeout)


# ------------------------------------------------ 测试 1: 启动加载多 Monitor
@pytest.mark.asyncio
async def test_start_scans_enabled_monitors_and_skips_disabled(tmp_path):
    db, (mon_a, mon_b, mon_c) = make_db(
        tmp_path,
        make_monitor_fields("A", interval_minutes=60),
        make_monitor_fields("B", interval_minutes=60),
        make_monitor_fields("C", interval_minutes=60, enabled=False),
    )
    scan, state = make_scan()
    events = {m.id: register_event(state, m.id) for m in (mon_a, mon_b, mon_c)}
    scheduler = MonitorScheduler(scan)
    scheduler.start(db.list_monitors())
    await asyncio.gather(wait_event(events[mon_a.id]), wait_event(events[mon_b.id]))
    await asyncio.sleep(0.3)  # give any (wrong) C scan a chance to appear

    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)) is not None
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_b.id)) is not None
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_c.id)) is None
    assert mon_a.id in state["runs"]      # A scanned immediately
    assert mon_b.id in state["runs"]      # B scanned immediately
    assert mon_c.id not in state["runs"]  # C never scanned
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 2: 每个 Monitor interval 独立
@pytest.mark.asyncio
async def test_job_intervals_match_monitor_config(tmp_path):
    db, (mon_a, mon_b, mon_c) = make_db(
        tmp_path,
        make_monitor_fields("A", interval_minutes=5),
        make_monitor_fields("B", interval_minutes=30),
        make_monitor_fields("C", interval_minutes=240),
    )
    scan, _ = make_scan()
    scheduler = MonitorScheduler(scan)
    for mon in (mon_a, mon_b, mon_c):
        scheduler.schedule(mon)
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)).trigger.interval == timedelta(minutes=5)
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_b.id)).trigger.interval == timedelta(minutes=30)
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_c.id)).trigger.interval == timedelta(minutes=240)
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 3: 同一 Monitor 不重叠
@pytest.mark.asyncio
async def test_same_monitor_never_overlaps(tmp_path):
    db, (mon_a,) = make_db(
        tmp_path, make_monitor_fields("A", interval_minutes=0.02)  # ~1.2 s
    )
    scan, state = make_scan(sleep=1.5)  # scan takes longer than the interval
    scheduler = MonitorScheduler(scan)
    scheduler.start(db.list_monitors())
    await asyncio.sleep(5.0)
    await scheduler.stop()
    assert state["peak_active"] == 1    # never two concurrent A scans
    assert len(state["runs"]) >= 2      # immediate + at least one tick
    db.close()


# --------------------------------------------- 测试 4: 不同 Monitor 可并行
@pytest.mark.asyncio
async def test_different_monitors_run_in_parallel(tmp_path):
    db, (mon_a, mon_b) = make_db(
        tmp_path,
        make_monitor_fields("A", interval_minutes=60),
        make_monitor_fields("B", interval_minutes=60),
    )
    scan, state = make_scan(sleep=0.5)
    events = {m.id: register_event(state, m.id) for m in (mon_a, mon_b)}
    scheduler = MonitorScheduler(scan)
    scheduler.start(db.list_monitors())
    await asyncio.gather(wait_event(events[mon_a.id]), wait_event(events[mon_b.id]))
    assert state["peak_active"] == 2  # A and B ran at the same time
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 5: 禁用 Monitor
@pytest.mark.asyncio
async def test_disable_stops_scans_but_keeps_data(tmp_path):
    db, (mon_a,) = make_db(
        tmp_path, make_monitor_fields("A", interval_minutes=0.05)  # ~3 s
    )
    db.add_monitor_product(mon_a.id, mercari_id="mX", title="T", price=1, url="u")
    db.ignore_product(mon_a.id, "mY")
    scan, state = make_scan()
    scheduler = MonitorScheduler(scan)
    scheduler.start(db.list_monitors())
    await asyncio.sleep(3.5)
    scheduler.disable_monitor(mon_a.id)
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)) is None
    count = len(state["runs"])
    await asyncio.sleep(3.2)
    assert len(state["runs"]) == count  # no further runs
    # history/ignored data untouched
    assert db.has_monitor_product(mon_a.id, "mX")
    assert db.is_ignored(mon_a.id, "mY")
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 6: 重新启用
@pytest.mark.asyncio
async def test_enable_scans_immediately_and_reschedules(tmp_path):
    db, (mon_a,) = make_db(
        tmp_path, make_monitor_fields("A", interval_minutes=60, enabled=False)
    )
    scan, state = make_scan()
    event = register_event(state, mon_a.id)
    scheduler = MonitorScheduler(scan)
    scheduler.start(db.list_monitors())
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)) is None

    db.set_monitor_enabled(mon_a.id, True)
    enabled = db.get_monitor(mon_a.id)
    await scheduler.enable_monitor(enabled)
    await wait_event(event)
    assert mon_a.id in state["runs"]  # immediate scan ran
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)) is not None
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 7: 修改 interval
@pytest.mark.asyncio
async def test_interval_change_reschedules_single_job(tmp_path):
    db, (mon_a,) = make_db(tmp_path, make_monitor_fields("A", interval_minutes=5))
    scan, _ = make_scan()
    scheduler = MonitorScheduler(scan)
    scheduler.schedule(mon_a)
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)).trigger.interval == timedelta(minutes=5)

    db.update_monitor(
        mon_a.id,
        **{**make_monitor_fields("A", interval_minutes=30), "keywords": "kw A",
           "keyword_mode": "AND", "filter_words": "", "ai_requirement": "",
           "notification_email": None},
    )
    updated = db.get_monitor(mon_a.id)
    scheduler.schedule(updated)
    jobs = scheduler._scheduler.get_jobs()
    assert len(jobs) == 1  # no duplicate job
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)).trigger.interval == timedelta(minutes=30)
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 8: reload 幂等
@pytest.mark.asyncio
async def test_reload_monitors_is_idempotent(tmp_path):
    db, (mon_a, mon_b, mon_c) = make_db(
        tmp_path,
        make_monitor_fields("A"),
        make_monitor_fields("B"),
        make_monitor_fields("C", enabled=False),
    )
    scan, _ = make_scan()
    scheduler = MonitorScheduler(scan)
    for _ in range(3):
        scheduler.reload_monitors(db.list_monitors())
    jobs = scheduler._scheduler.get_jobs()
    job_ids = [job.id for job in jobs]
    assert job_ids.count(scheduler.job_id(mon_a.id)) == 1
    assert job_ids.count(scheduler.job_id(mon_b.id)) == 1
    assert scheduler.job_id(mon_c.id) not in job_ids
    assert len(jobs) == 2
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 9: 异常不影响其他 Monitor
@pytest.mark.asyncio
async def test_failed_scan_does_not_affect_other_monitors(tmp_path, caplog):
    db, (mon_a, mon_b) = make_db(
        tmp_path,
        make_monitor_fields("A", interval_minutes=0.05),  # ~3 s
        make_monitor_fields("B", interval_minutes=0.05),
    )
    scan, state = make_scan(fail_ids={mon_a.id})
    scheduler = MonitorScheduler(scan)
    scheduler.start(db.list_monitors())
    await asyncio.sleep(3.6)
    await scheduler.stop()
    assert state["runs"].count(mon_b.id) >= 2  # B kept running on its ticks
    assert mon_a.id in state["runs"]           # A ran (and failed)
    assert "扫描失败" in caplog.text
    db.close()


# --------------------------------------------- 测试 10: 程序重启语义
@pytest.mark.asyncio
async def test_restart_with_new_instance_rescans_enabled(tmp_path):
    db, (mon_a, mon_b, mon_c) = make_db(
        tmp_path,
        make_monitor_fields("A", interval_minutes=60),
        make_monitor_fields("B", interval_minutes=60),
        make_monitor_fields("C", interval_minutes=60, enabled=False),
    )
    scan, state = make_scan()
    first = MonitorScheduler(scan)
    first.start(db.list_monitors())
    await asyncio.sleep(0.4)
    await first.stop()

    second = MonitorScheduler(scan)
    second.start(db.list_monitors())
    await asyncio.sleep(0.4)
    await second.stop()

    assert state["runs"].count(mon_a.id) == 2  # immediate scan in both instances
    assert state["runs"].count(mon_b.id) == 2
    assert mon_c.id not in state["runs"]       # disabled never scans
    db.close()


# --------------------------------------------- 测试 11: Job ID 稳定
@pytest.mark.asyncio
async def test_job_id_stable_across_rename(tmp_path):
    db, (mon_a,) = make_db(tmp_path, make_monitor_fields("Old Name"))
    scan, _ = make_scan()
    scheduler = MonitorScheduler(scan)
    scheduler.schedule(mon_a)
    stable_id = scheduler.job_id(mon_a.id)
    assert stable_id == f"monitor-{mon_a.id}"

    db.update_monitor(
        mon_a.id,
        **{**make_monitor_fields("New Name"), "keywords": "kw New Name",
           "keyword_mode": "AND", "filter_words": "", "ai_requirement": "",
           "notification_email": None},
    )
    scheduler.reload_monitors(db.list_monitors())
    assert scheduler._scheduler.get_job(stable_id) is not None  # id unchanged
    assert len(scheduler._scheduler.get_jobs()) == 1
    await scheduler.stop()
    db.close()


# --------------------------------------------- 测试 12: 删除 Monitor
@pytest.mark.asyncio
async def test_remove_monitor_only_removes_its_own_job(tmp_path):
    db, (mon_a, mon_b) = make_db(
        tmp_path,
        make_monitor_fields("A"),
        make_monitor_fields("B"),
    )
    scan, _ = make_scan()
    scheduler = MonitorScheduler(scan)
    scheduler.reload_monitors(db.list_monitors())
    scheduler.remove_monitor(mon_a.id)
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_a.id)) is None
    assert scheduler._scheduler.get_job(scheduler.job_id(mon_b.id)) is not None
    await scheduler.stop()
    db.close()
