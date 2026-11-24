# -*- coding: utf-8 -*-
"""Long-running monitor with MonitorScheduler (Phase 5.3-B wiring).

One TEMP monitor is created in data/phase3_test.db (the real production
database is never touched). On startup the scheduler registers the
monitor's job and immediately scans it once; afterwards it scans every
INTERVAL_MINUTES.

Usage (from the project root):
    python scripts/test_phase3.py            # start the monitor
    python scripts/test_phase3.py --once     # run a single scan and exit
Ctrl+C stops it gracefully.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.database import Database, Monitor  # noqa: E402
from app.deepseek import DeepSeekClient  # noqa: E402
from app.outbox import MailOutbox  # noqa: E402
from app.mercari import MercariClient  # noqa: E402
from app.scan_service import load_scan_budgets, make_scan_handler  # noqa: E402
from app.scheduler import MonitorScheduler  # noqa: E402
from app.single_instance import InstanceLock  # noqa: E402

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "phase3_test.db"
MONITOR_NAME = "Phase3 Test Monitor"
INTERVAL_MINUTES = 30

log = logging.getLogger("monitor")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def ensure_monitor(db: Database) -> Monitor:
    """Create the temp test monitor once (idempotent across restarts)."""
    mon = db.get_monitor_by_name(MONITOR_NAME)
    if mon is None:
        monitor_id = db.create_monitor(
            name=MONITOR_NAME,
            keywords="Built to Spill",
            keyword_mode="AND",
            min_price=1000,
            max_price=5000,
            filter_words="LP，DVD，Blu-ray",
            ai_requirement="必须是 CD，不要 LP、DVD 或数字版",
            interval_minutes=INTERVAL_MINUTES,
            notification_email=None,
            enabled=True,
        )
        mon = db.get_monitor(monitor_id)
    return mon


async def run_single() -> int:
    """--once mode: one scan, then exit."""
    mercari_budget, deepseek_budget = load_scan_budgets()
    db = Database(TEST_DB)
    client = MercariClient(budget=mercari_budget)
    outbox = MailOutbox(db)
    outbox.recover()
    try:
        async with httpx.AsyncClient(timeout=30.0) as ai_client:
            mon = ensure_monitor(db)
            scan = make_scan_handler(db, client, DeepSeekClient(ai_client, deepseek_budget), outbox)
            summary = await scan(mon.id)
            await outbox.drain_due()
            if summary is None:
                log.error("Monitor %s did not produce a scan summary", mon.id)
                return 1
            log.info("%s", summary)
            return 0
    finally:
        db.close()
        await client.close()


async def run_monitor() -> int:
    """Long-running mode: scheduler + immediate scan + periodic scans."""
    mercari_budget, deepseek_budget = load_scan_budgets()
    db = Database(TEST_DB)
    client = MercariClient(budget=mercari_budget)
    ai_client = httpx.AsyncClient(timeout=30.0)
    outbox = MailOutbox(db)
    ensure_monitor(db)
    deepseek = DeepSeekClient(ai_client, deepseek_budget)
    scheduler = MonitorScheduler(make_scan_handler(db, client, deepseek, outbox))
    outbox.start()
    scheduler.start(db.list_monitors())
    log.info(
        "Scheduler started (monitor %r, every %d minutes)",
        MONITOR_NAME,
        INTERVAL_MINUTES,
    )

    try:
        await asyncio.Event().wait()  # run until Ctrl+C
    finally:
        log.info("Stopping...")
        await scheduler.stop()
        await outbox.stop()
        db.close()
        await client.close()
        await ai_client.aclose()
        log.info("Stopped. Bye.")
    return 0


def main() -> int:
    setup_logging()
    TEST_DB.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(TEST_DB.with_suffix(".db.lock"))
    if not lock.acquire():
        log.error("The Phase 3 test database is already in use")
        return 1
    try:
        if "--once" in sys.argv:
            log.info("Single-scan mode (--once)")
            try:
                return asyncio.run(run_single())
            except Exception:
                log.exception("Single scan failed")
                return 1
        log.info("Starting MercariJapanMonitor phase3 test (Ctrl+C to stop)")
        return asyncio.run(run_monitor())
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
