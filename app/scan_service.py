"""Shared scan wiring for the GUI and command-line checks."""

from collections.abc import Awaitable, Callable

from app.database import Database
from app.deepseek import DeepSeekClient
from app.mercari import MercariClient
from app.outbox import MailOutbox
from app.request_budget import RequestBudget
from app.scanner import ScanResult, scan_monitor


def load_scan_budgets() -> tuple[RequestBudget, RequestBudget]:
    return (
        RequestBudget.from_env("MJM_MERCARI_REQUESTS_PER_MINUTE", 60),
        RequestBudget.from_env("MJM_DEEPSEEK_REQUESTS_PER_MINUTE", 30),
    )


async def scan_monitor_by_id(
    db: Database,
    client: MercariClient,
    deepseek: DeepSeekClient,
    outbox: MailOutbox,
    monitor_id: int,
    *,
    ai_limit: int | None = None,
) -> ScanResult | None:
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        return None
    return await scan_monitor(
        client, db, monitor, judge=deepseek.make_judge(monitor.ai_requirement),
        outbox=outbox, ai_limit=ai_limit,
    )


def make_scan_handler(
    db: Database, client: MercariClient, deepseek: DeepSeekClient, outbox: MailOutbox,
) -> Callable[[int], Awaitable[str | None]]:
    async def scan(monitor_id: int) -> str | None:
        result = await scan_monitor_by_id(db, client, deepseek, outbox, monitor_id)
        return result.summary if result is not None else None
    return scan
