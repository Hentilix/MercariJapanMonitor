"""Discover and judge durable candidates for one monitor scan."""

import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING
from time import time

from app.coverage import AI_PER_ROUND, discover

from app.database import Database, Monitor, split_keywords
from app.deepseek import DeepSeekJudge, DeepSeekUnavailable
from app.filter import FilterRule, passes
from app.mercari import ItemDetail, MercariClient, MercariError, SearchItem
from app.request_budget import RequestDeferred, guard_requests

if TYPE_CHECKING:
    from app.outbox import MailOutbox

log = logging.getLogger(__name__)

# A judge receives the extracted detail fields and returns
# True / False / None (None = error, never a match).
Judge = Callable[[ItemDetail], Awaitable[bool | None]]


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
    summary: str | None = None


def _fmt_price(price: int | None) -> str:
    return "no price" if price is None else f"\u00a5{price:,}"


async def scan_monitor(
    client: MercariClient,
    db: Database,
    monitor: Monitor,
    *,
    judge: Judge | DeepSeekJudge | None = None,
    outbox: MailOutbox | None = None,
    ai_limit: int | None = None,
) -> ScanResult:
    """Discover candidates, process due listings, and save the scan summary."""
    if not db.rule_is_current(monitor):
        return ScanResult()
    mail_batch = outbox.new_batch(monitor) if outbox is not None else None
    keywords = split_keywords(monitor.keywords)
    exclude_words = split_keywords(monitor.filter_words)
    rule = FilterRule(
        keywords=keywords,
        exclude_keywords=exclude_words,
        min_price=monitor.min_price,
        max_price=monitor.max_price,
        match_mode=monitor.keyword_mode,
    )

    managed_judge = judge if isinstance(judge, DeepSeekJudge) else None

    def ai_pause_reason() -> str:
        if managed_judge is not None:
            return managed_judge.pause_reason()
        if monitor.ai_requirement and judge is None:
            return "缺少 DeepSeek API Key"
        return ""

    coverage = await discover(client, db, monitor, rule, clock=time)
    if coverage is None:
        return ScanResult()

    pause_reason = ai_pause_reason()
    pending: list[SearchItem] = []
    if not pause_reason:
        limit = AI_PER_ROUND if ai_limit is None else max(0, min(ai_limit, AI_PER_ROUND))
        pending = [
            SearchItem(*row) for row in db.candidate_rows(
                monitor.id, time(), limit if monitor.ai_requirement else None,
            )
        ]
    result = ScanResult(
        found=coverage.found, candidates=coverage.candidates, new=coverage.queued,
        already_seen=coverage.seen, ignored=coverage.ignored,
    )

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

    def can_process() -> bool:
        return db.rule_is_current(monitor) and (
            db.search_is_current(monitor.id, coverage.generation)
        )

    def can_judge(item: SearchItem) -> bool:
        return can_process() and not db.is_ignored(monitor.id, item.id) and (
            db.has_candidate(monitor.id, item.id)
        )

    def record(item: SearchItem, matched: bool) -> None:
        if not can_judge(item):
            return
        inserted = db.add_monitor_product(
            monitor.id, mercari_id=item.id, title=item.title, price=item.price,
            url=item.url, published_at=item.published_at, matched=matched,
            mail_batch=mail_batch, rule_version=monitor.rule_version,
        )

        if not inserted:
            return
        if matched:
            result.ai_matched += 1
            log.info("MATCH: %s | %s | %s", item.id, item.title, _fmt_price(item.price))
        else:
            result.ai_rejected += 1

    try:
        for item in pending:
            if not can_process():
                break
            if not can_judge(item):
                continue
            pause_reason = ai_pause_reason()
            if pause_reason:
                break
            previous_errors = result.ai_errors
            try:
                with guard_requests(lambda: can_judge(item)):
                    attempt = managed_judge.attempt() if managed_judge else nullcontext(judge)
                    with attempt as judge_detail:
                        detail = None
                        if item.id not in coverage.current_candidates:
                            listing = await client.get_current_listing(item.id, item.item_type)
                            if not can_judge(item):
                                continue
                            if listing is None:
                                db.discard_candidate(monitor.id, item.id)
                                continue
                            price, detail = listing
                            result.details_fetched += 1
                            if price is None and (rule.min_price is not None or rule.max_price is not None):
                                raise MercariError("Current listing price is unavailable")
                            item = replace(item, title=detail.title, price=price)
                        if not passes(item, rule):
                            db.discard_candidate(monitor.id, item.id)
                            continue
                        if detail is not None:
                            db.refresh_candidate(
                                monitor.id, item.id, item.title, item.price, json.dumps(asdict(detail)),
                            )
                        if judge_detail is None:
                            record(item, matched=True)
                        else:
                            await _judge_monitor_item(
                                client, judge_detail, item, record, result,
                                lambda: can_judge(item), db, monitor,
                                coverage.generation,
                                detail=detail,
                            )
            except DeepSeekUnavailable:
                result.ai_errors += 1
                pause_reason = ai_pause_reason()
                break
            except MercariError as exc:
                log.warning("Listing verification failed for %s: %s", item.id, exc)
                result.ai_errors += 1
            except RequestDeferred:
                pause_reason = ai_pause_reason()
                if pause_reason:
                    break
            if result.ai_errors > previous_errors and can_judge(item):
                db.defer_candidate(
                    monitor.id, item.id, time(),
                    rule_version=monitor.rule_version,
                )
    finally:
        if mail_batch is not None:
            outbox.finish_batch(mail_batch)

    summary = (
        f"found={result.found}, candidates={result.candidates}, "
        f"new={result.new}, already_seen={result.already_seen}, "
        f"matched={result.ai_matched}, "
        f"rejected={result.ai_rejected}, ignored={result.ignored}, "
        f"errors={result.ai_errors}"
    )
    if result.found > 0 and result.candidates == 0:
        # P1-2 fix: make the "everything was filtered out" case obvious
        # instead of leaving the user with only a raw key=value dump.
        summary += (
            f"（找到 {result.found} 件，但全部被 Level 1 过滤："
            "请检查关键词、价格范围或排除词）"
        )
    if pause_reason:
        summary += f" · AI 暂停（{pause_reason}）"
    if not db.search_is_current(monitor.id, coverage.generation):
        return result
    summary = (
        f"{coverage.summary()} · 待处理候选 {db.pending_candidates(monitor.id)} 件 | "
        + summary
    )
    if db.set_last_scan(monitor.id, summary, rule_version=monitor.rule_version):
        result.summary = summary
        log.info("Monitor %s (%s): %s", monitor.id, monitor.name, summary)
    return result


async def _judge_monitor_item(
    client, judge, item, record, result, can_process, db, monitor, generation, *, detail=None,
) -> None:
    if detail is None:
        cached_detail = db.candidate_detail(monitor.id, item.id)
        if cached_detail is not None:
            detail = ItemDetail(**json.loads(cached_detail))
    if detail is None:
        try:
            detail = await client.get_item_details(item.id, item.item_type)
        except MercariError as exc:
            log.error("Detail fetch failed for %s: %s", item.id, exc)
            result.ai_errors += 1
            return
        if detail is None:
            log.warning("Detail unavailable for %s (sold or deleted)", item.id)
            result.ai_errors += 1
            return
        result.details_fetched += 1
        if not can_process():
            return
        db.save_candidate_detail(monitor, generation, item.id, json.dumps(asdict(detail)))

    try:
        verdict = await judge(detail)
    except (RequestDeferred, DeepSeekUnavailable):
        raise
    except Exception as exc:
        log.error("AI judge crashed for %s: %s", item.id, exc)
        result.ai_errors += 1
        return  # NOT processed -> retried next scan

    if verdict is True:
        record(item, matched=True)
    elif verdict is False:
        record(item, matched=False)
    else:
        result.ai_errors += 1
        log.error("AI judge returned no valid verdict for %s", item.id)
        # NOT processed -> retried next scan
