# -*- coding: utf-8 -*-
"""MercariJapanMonitor — application entry point.

Run from the project root:
    python main.py
then open http://127.0.0.1:8081 in the browser.

The port can be overridden with the MJM_PORT environment variable
(default 8081 — 8080 is frequently taken by Steam/other dev tools).
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import httpx
from nicegui import app, ui

from app.database import Database
from app.deepseek import DeepSeekClient
from app.mercari import MercariClient
from app.outbox import MailOutbox
from app.scan_service import load_scan_budgets, make_scan_handler
from app.scheduler import MonitorScheduler
from app.single_instance import InstanceLock
from app.ui import register_pages

log = logging.getLogger("gui")

PORT = int(os.environ.get("MJM_PORT", "8081"))
LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"


class _MercapiOptionalFieldFilter(logging.Filter):
    """Drop mercapi's "optional response property ... could not be parsed"
    warnings.

    They fire when an OPTIONAL API field is null/missing (verified live:
    e.g. Shops products with "brand": null). Those fields (brand,
    productStats) are not used by this project — the fields we do use
    (title/description/condition/category) parse fine — so the warning is
    pure console noise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Encountered optional response property" not in record.getMessage()


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                LOG_DIR / "monitor.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        print(f"[MJM] 无法创建日志目录 {LOG_DIR}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger().addFilter(_MercapiOptionalFieldFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    setup_logging()
    try:
        mercari_budget, deepseek_budget = load_scan_budgets()
    except ValueError as exc:
        log.error("%s", exc)
        return 1
    instance_lock = InstanceLock()
    if not instance_lock.acquire():
        log.warning(
            "另一个 MarketplaceMonitor 实例正在运行（锁文件 %s），本次启动直接退出。",
            instance_lock.path,
        )
        print(
            "[MJM] 检测到已有 MarketplaceMonitor 实例在运行，本次启动退出。",
            file=sys.stderr,
        )
        return 1
    try:
        db = Database()
    except BaseException:
        instance_lock.release()
        raise
    mail_outbox = MailOutbox(db)
    client = MercariClient(budget=mercari_budget)
    ai_http = httpx.AsyncClient(timeout=30.0)
    deepseek = DeepSeekClient(ai_http, deepseek_budget)
    scheduler = MonitorScheduler(make_scan_handler(db, client, deepseek, mail_outbox))
    register_pages(db, scheduler)

    @app.on_startup
    async def startup() -> None:
        mail_outbox.start()
        monitors = db.list_monitors()
        for mon in monitors:
            if mon.enabled:
                log.info("Monitor %s (%s) 已启用", mon.id, mon.name)
            else:
                log.info("Monitor %s (%s) 已禁用（不扫描、不调度）", mon.id, mon.name)
        # Registers one job per enabled monitor and immediately scans each
        # enabled monitor once (fire-and-forget, independent per monitor).
        scheduler.start(monitors)
        enabled_count = sum(1 for m in monitors if m.enabled)
        log.info(
            "Scheduler started: %d monitor(s), %d enabled. GUI at http://127.0.0.1:%d",
            len(monitors),
            enabled_count,
            PORT,
        )

    @app.on_shutdown
    async def shutdown() -> None:
        log.info("Shutting down...")
        await scheduler.stop()
        await mail_outbox.stop()
        db.close()
        await client.close()
        await ai_http.aclose()
        log.info("Stopped. Bye.")

    try:
        ui.run(
            title="MercariJapanMonitor",
            host="127.0.0.1",
            port=PORT,
            reload=False,
            show=False,
            language="zh-CN",
        )
    finally:
        instance_lock.release()
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
