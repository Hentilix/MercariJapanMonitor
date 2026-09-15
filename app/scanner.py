"""One complete scan cycle for ONE monitor (Phase 5.3-A final).

Monitor-centric scan: processing state lives in monitor_products /
ignored_products keyed by (monitor_id, mercari_id).

"Processed" semantics:
  * without AI: Level 1 pass == processing done -> recorded as matched
  * with AI:    full_item() success + an explicit true/false verdict
                == processing done -> only then recorded
  Items whose full_item() or AI judgement failed are NOT recorded, so the
  next scan sees them as new again and retries them.

Calling scan_monitor() once = one full scan. MonitorScheduler calls it
repeatedly.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.database import Database, Monitor, split_keywords
from app.filter import FilterRule, apply_filter
from app.mercari import ItemDetail, MercariClient, MercariError, SearchItem

log = logging.getLogger(__name__)

# A judge receives the extracted detail fields and returns
# True / False / None (None = error, never a match).
Judge = Callable[[ItemDetail], Awaitable[bool | None]]
# A notifier receives ALL AI matches of one scan and sends ONE summary
# notification (e.g. one email per scan task per round).
Notifier = Callable[[list[SearchItem]], Awaitable[None]]


@dataclass
class ScanResult:
    found: int = 0
    candidates: int = 0
    new: int = 0
    already_seen: int = 0
    ignored: int = 0
    details_fetched: int = 0
    ai_matched: int = 0
    ai_rejected: int = 0
    ai_errors: int = 0


def _fmt_price(price: int | None) -> str:
    return "no price" if price is None else f"\u00a5{price:,}"


async def scan_monitor(
    client: MercariClient,
    db: Database,
    monitor: Monitor,
    *,
    judge: Judge | None = None,
    notifier: Notifier | None = None,
    max_pages: int = 2,
    ai_limit: int | None = None,
) -> ScanResult:
    """One full scan for ONE monitor.

    Flow:
        FilterRule from monitor config -> Mercari search -> Level 1
        -> skip ignored -> skip already processed -> full_item -> DeepSeek
        -> true:  history (monitor_products.matched=1) + batch email
        -> false: processed-but-unmatched (matched=0, never re-judged,
                  never in the history)
        -> error: nothing saved, retried next scan
        -> one batch email per scan; last_scan_at/last_result updated.

    A Mercari search failure records a failure status and re-raises
    (the scheduler/caller decides what to do next).
    """
    rule = FilterRule(
        keywords=split_keywords(monitor.keywords),
        exclude_keywords=split_keywords(monitor.filter_words),
        min_price=monitor.min_price,
        max_price=monitor.max_price,
        match_mode=monitor.keyword_mode,
    )

    try:
        products = await client.search_products(monitor.keywords, max_pages=max_pages)
    except MercariError as exc:
        db.set_last_scan(
            monitor.id, f"搜索失败: {type(exc).__name__}: {exc}"
        )
        log.error("Monitor %s (%s) search failed: %s", monitor.id, monitor.name, exc)
        raise

    candidates = apply_filter(products, rule)
    result = ScanResult(found=len(products), candidates=len(candidates))

    pending: list[SearchItem] = []
    for item in candidates:
        if db.is_ignored(monitor.id, item.id):
            result.ignored += 1
        elif db.has_monitor_product(monitor.id, item.id):
            result.already_seen += 1
        else:
            result.new += 1
            pending.append(item)

    log.info(
        "Monitor %s (%s) scan: found=%d, candidates=%d, new=%d, "
        "already_seen=%d, ignored=%d",
        monitor.id,
        monitor.name,
        result.found,
        result.candidates,
        result.new,
        result.already_seen,
        result.ignored,
    )

    targets = pending[:ai_limit] if ai_limit is not None else pending
    if ai_limit is not None and len(pending) > ai_limit:
        log.info(
            "Monitor %s: AI stage capped, processing %d of %d pending items",
            monitor.id,
            ai_limit,
            len(pending),
        )
    matched_items: list[SearchItem] = []

    if judge is None:
        # No AI gate: every unprocessed Level-1 candidate is a match.
        for item in targets:
            _record_monitor_product(db, monitor, item, matched=True)
            result.ai_matched += 1
            matched_items.append(item)
            log.info(
                "MATCH (no AI): %s | %s | %s",
                item.id,
                item.title,
                _fmt_price(item.price),
            )
    else:
        for item in targets:
            await _judge_monitor_item(
                client, db, monitor, judge, item, matched_items, result
            )

    if matched_items and notifier is not None:
        try:
            await notifier(matched_items)
        except Exception as exc:
            # A failed notification is not an AI error: matches still count
            # and the scan finishes normally.
            log.error(
                "Batch email notification failed (%d item(s)): %s",
                len(matched_items),
                exc,
            )

    summary = (
        f"found={result.found}, candidates={result.candidates}, "
        f"new={result.new}, matched={result.ai_matched}, "
        f"rejected={result.ai_rejected}, ignored={result.ignored}, "
        f"errors={result.ai_errors}"
    )
    db.set_last_scan(monitor.id, summary)
    log.info("Monitor %s (%s): %s", monitor.id, monitor.name, summary)
    return result


def _record_monitor_product(
    db: Database, monitor: Monitor, item: SearchItem, *, matched: bool
) -> None:
    db.add_monitor_product(
        monitor.id,
        mercari_id=item.id,
        title=item.title,
        price=item.price,
        url=item.url,
        published_at=item.published_at,
        matched=matched,
    )


async def _judge_monitor_item(
    client, db, monitor, judge, item, matched_items, result
) -> None:
    try:
        detail = await client.get_item_details(item.id, item.item_type)
    except MercariError as exc:
        log.error("Detail fetch failed for %s: %s", item.id, exc)
        result.ai_errors += 1
        return  # NOT processed -> the next scan retries this item
    if detail is None:
        log.warning("Detail unavailable for %s (sold or deleted)", item.id)
        result.ai_errors += 1
        return  # NOT processed -> retried if it ever shows up again
    result.details_fetched += 1

    try:
        verdict = await judge(detail)
    except Exception as exc:
        log.error("AI judge crashed for %s: %s", item.id, exc)
        result.ai_errors += 1
        return  # NOT processed -> retried next scan

    if verdict is True:
        _record_monitor_product(db, monitor, item, matched=True)
        result.ai_matched += 1
        matched_items.append(item)
        log.info("AI MATCH: %s | %s | %s", item.id, item.title, _fmt_price(item.price))
    elif verdict is False:
        # Processed, but must never show up in the history and never be
        # judged again: recorded with matched=0.
        _record_monitor_product(db, monitor, item, matched=False)
        result.ai_rejected += 1
    else:
        result.ai_errors += 1
        log.error("AI judge returned no valid verdict for %s", item.id)
        # NOT processed -> retried next scan
