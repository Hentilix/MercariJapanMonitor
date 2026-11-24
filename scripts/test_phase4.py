# -*- coding: utf-8 -*-
"""Phase 4-5.3 integration test — real Mercari + full_item() + DeepSeek,
driven through the Monitor model (scan_monitor).

Uses its OWN database (data/phase4_test.db), so the production
data/mercari_monitor.db is never touched. Requires DEEPSEEK_API_KEY_FOR_MJM.

Usage (from the project root):
    python scripts/test_phase4.py --limit 3

    --limit N   cap on how many new items are sent to DeepSeek
                (default 3; keeps the test cheap)

Re-runs see the same items as "already processed" — delete
data/phase4_test.db to force a fresh AI round.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.database import Database  # noqa: E402
from app.deepseek import DeepSeekClient, get_api_key  # noqa: E402
from app.outbox import MailOutbox  # noqa: E402
from app.mercari import MercariClient  # noqa: E402
from app.scan_service import load_scan_budgets, scan_monitor_by_id  # noqa: E402
from app.single_instance import InstanceLock  # noqa: E402

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "phase4_test.db"
MONITOR_NAME = "Phase4 Test Monitor"
SPECIAL_REQUIREMENT = "必须是 CD，不要 LP、DVD 或数字版"

log = logging.getLogger("phase4")


def ensure_monitor(db: Database):
    mon = db.get_monitor_by_name(MONITOR_NAME)
    if mon is None:
        monitor_id = db.create_monitor(
            name=MONITOR_NAME,
            keywords="Built to Spill",
            keyword_mode="AND",
            min_price=1000,
            max_price=5000,
            filter_words="LP，DVD，Blu-ray",
            ai_requirement=SPECIAL_REQUIREMENT,
            interval_minutes=30,
            notification_email=None,
            enabled=True,
        )
        mon = db.get_monitor(monitor_id)
    return mon


async def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 integration test")
    parser.add_argument(
        "--limit", type=int, default=3,
        help="max new items to send to DeepSeek (default: 3)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not get_api_key():
        print("[ERROR] DEEPSEEK_API_KEY_FOR_MJM is not set.")
        print('Set it first, e.g. PowerShell:  $env:DEEPSEEK_API_KEY_FOR_MJM = "sk-..."')
        return 1

    TEST_DB.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(TEST_DB.with_suffix(".db.lock"))
    if not lock.acquire():
        log.error("The Phase 4 test database is already in use")
        return 1
    try:
        return await run_scan(args.limit)
    finally:
        lock.release()


async def run_scan(limit: int) -> int:
    mercari_budget, deepseek_budget = load_scan_budgets()
    db = Database(TEST_DB)
    client = MercariClient(budget=mercari_budget)
    mon = ensure_monitor(db)
    outbox = MailOutbox(db)
    outbox.recover()
    try:
        async with httpx.AsyncClient(timeout=30.0) as ai_http:
            deepseek = DeepSeekClient(ai_http, deepseek_budget)
            result = await scan_monitor_by_id(db, client, deepseek, outbox, mon.id, ai_limit=limit)
    finally:
        try:
            await outbox.drain_due()
        finally:
            db.close()
            await client.close()

    if result is None or result.summary is None:
        log.error("Monitor %s did not produce a scan summary", mon.id)
        return 1
    log.info("%s", result.summary)

    print("\n" + "=" * 40)
    print(f"Found: {result.found}")
    print(f"Level 1 candidates: {result.candidates}")
    print(f"New: {result.new}")
    print(f"Ignored: {result.ignored}")
    print(f"AI matched: {result.matched}")
    print(f"AI rejected: {result.rejected}")
    print(f"AI errors: {result.ai_errors}")
    print("=" * 40)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
