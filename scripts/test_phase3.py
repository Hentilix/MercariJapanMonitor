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
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.database import Database, Monitor  # noqa: E402
from app.deepseek import judge_item  # noqa: E402
from app.email import make_match_notifier  # noqa: E402
from app.mercari import MercariClient  # noqa: E402
from app.scanner import scan_monitor  # noqa: E402
from app.scheduler import MonitorScheduler  # noqa: E402

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


def make_judge(ai_client: httpx.AsyncClient, requirement: str):
    """Build the AI judge, or None when no API key is configured."""
    if not requirement or not os.environ.get("DEEPSEEK_API_KEY_FOR_MJM"):
        if requirement:
            log.warning("DEEPSEEK_API_KEY_FOR_MJM is not set — AI filtering skipped")
        return None

    async def judge(detail):
        return await judge_item(ai_client, requirement, detail)

    return judge


async def scan_by_id(
    monitor_id: int, db: Database, client: MercariClient, ai_client: httpx.AsyncClient
) -> None:
    mon = db.get_monitor(monitor_id)
    if mon is None:
        return
    judge = make_judge(ai_client, mon.ai_requirement)
    notifier = make_match_notifier(label=mon.name, recipient=mon.notification_email)
    try:
        await scan_monitor(client, db, mon, judge=judge, notifier=notifier)
    except Exception:
        log.exception("Monitor %s (%s) 扫描失败", mon.id, mon.name)


async def run_single() -> int:
    """--once mode: one scan, then exit."""
    db = Database(TEST_DB)
    client = MercariClient()
    try:
        async with httpx.AsyncClient(timeout=30.0) as ai_client:
            mon = ensure_monitor(db)
            await scan_by_id(mon.id, db, client, ai_client)
            return 0
    finally:
        db.close()
        await client.close()


async def run_monitor() -> int:
    """Long-running mode: scheduler + immediate scan + periodic scans."""
    db = Database(TEST_DB)
    client = MercariClient()
    ai_client = httpx.AsyncClient(timeout=30.0)
    ensure_monitor(db)
    scheduler = MonitorScheduler(
        lambda monitor_id: scan_by_id(monitor_id, db, client, ai_client)
    )
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
        db.close()
        await client.close()
        await ai_client.aclose()
        log.info("Stopped. Bye.")
    return 0


def main() -> int:
    setup_logging()
    if "--once" in sys.argv:
        log.info("Single-scan mode (--once)")
        return asyncio.run(run_single())
    log.info("Starting MercariJapanMonitor phase3 test (Ctrl+C to stop)")
    return asyncio.run(run_monitor())


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
