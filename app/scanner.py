"""One complete scan cycle for ONE monitor (Phase 5.3-A final).

Monitor-centric scan: processing state lives in monitor_products /
ignored_products keyed by (monitor_id, mercari_id).

"Processed" semantics:
  * without AI (ai_requirement empty): Level 1 pass == processing done
    -> recorded as matched
  * with AI (ai_requirement non-empty): full_item() success + an explicit
    true/false verdict == processing done -> only then recorded.
  If AI is unavailable, candidates stay pending. Successful detail
  fetches are saved with adaptive candidates for later judgment.

Calling scan_monitor() once = one full scan. MonitorScheduler calls it
repeatedly.
"""

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
from app.filter import FilterRule, apply_filter, passes
from app.mercari import ItemDetail, MercariClient, MercariError, SearchItem
from app.request_budget import RequestDeferred, guard_requests

if TYPE_CHECKING:
    from app.outbox import MailOutbox

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
    summary: str | None = None


def _fmt_price(price: int | None) -> str:
    return "no price" if price is None else f"\u00a5{price:,}"


async def scan_monitor(
    client: MercariClient,
    db: Database,
    monitor: Monitor,
    *,
    judge: Judge | DeepSeekJudge | None = None,
    notifier: Notifier | None = None,
    outbox: MailOutbox | None = None,
    max_pages: int = 2,
    adaptive: bool = False,
    ai_limit: int | None = None,
) -> ScanResult:
    """One full scan for ONE monitor.

    Flow:
        FilterRule from monitor config -> Mercari search -> Level 1
        -> skip ignored -> skip already processed -> full_item -> DeepSeek
        -> true:  history (monitor_products.matched=1) + batch email
        -> false: rejected under this rule version (matched=0)
        -> error: candidate remains pending for retry
        -> one batch email per scan; last_scan_at/last_result updated.

    A Mercari search failure records a failure status and re-raises
    (the scheduler/caller decides what to do next).
    """
    if outbox is not None and notifier is not None:
        raise ValueError("Choose one notification mechanism")
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

    pause_reason = ""
    coverage = None
    if adaptive:
        coverage = await discover(client, db, monitor, rule, clock=time)
        if coverage is None:
            return ScanResult()
        pause_reason = ai_pause_reason()
        limit = min(ai_limit, AI_PER_ROUND) if ai_limit is not None else AI_PER_ROUND
        pending = []
        if not pause_reason:
            pending = [
                SearchItem(*row) for row in db.candidate_rows(
                    monitor.id, time(), max(0, limit) if monitor.ai_requirement else None
                )
            ]
        result = ScanResult(
            found=coverage.found, candidates=coverage.candidates, new=coverage.queued,
            already_seen=coverage.seen, ignored=coverage.ignored,
        )
    else:
        # P1-1 fix: push the monitor's price range and exclude words into the
        # Mercari search itself, so the first pages are not wasted on items the
        # local Level 1 would reject anyway. mercapi 0.5.0 accepts ONE exclude
        # string (excludeKeyword), so the words are space-joined; the local
        # FilterRule above stays the final authority for per-word exclusion.
        search_exclude = " ".join(exclude_words) or None

        try:
            if monitor.keyword_mode.upper() == "OR":
                # P0-1 follow-up: OR mode must NOT space-join all keywords into
                # one query — Mercari treats a joined multi-word query as ~AND
                # and returns zero results for OR monitors (verified live:
                # 4 joined words -> num_found=0 vs 1461/40/1573 per single
                # keyword). Search each keyword separately and merge with
                # id-deduplication; local Level 1 keeps the final OR decision.
                products: list[SearchItem] = []
                seen: set[str] = set()
                for kw in keywords:
                    batch = await client.search_products(
                        kw,
                        min_price=monitor.min_price,
                        max_price=monitor.max_price,
                        exclude=search_exclude,
                        max_pages=max_pages,
                    )
                    for item in batch:
                        if item.id not in seen:
                            seen.add(item.id)
                            products.append(item)
            else:
                # P0-1 fix: the Mercari query is the SPACE-JOINED keywords
                # (AND mode: every keyword should match). The raw string (with
                # Chinese commas) went to Mercari verbatim and crushed recall
                # (verified live: "Waltz For Debby，Bill Evans" -> 15 results,
                # "Waltz For Debby Bill Evans" -> 242). Level 1 keeps the
                # comma-split keyword list above — never the joined phrase.
                search_query = " ".join(keywords)
                products = await client.search_products(
                    search_query,
                    min_price=monitor.min_price,
                    max_price=monitor.max_price,
                    exclude=search_exclude,
                    max_pages=max_pages,
                )
        except MercariError as exc:
            db.set_last_scan(
                monitor.id, f"搜索失败: {type(exc).__name__}: {exc}",
                rule_version=monitor.rule_version,
            )
            log.error("Monitor %s (%s) search failed: %s", monitor.id, monitor.name, exc)
            raise

        candidates = apply_filter(products, rule)
        result = ScanResult(found=len(products), candidates=len(candidates))

        pending: list[SearchItem] = []
        pending_ids: set[str] = set()
        for item in candidates:
            if db.is_ignored(monitor.id, item.id):
                result.ignored += 1
            elif item.id in pending_ids or not monitor.needs_judgment(
                db.product_decision(monitor.id, item.id)
            ):
                result.already_seen += 1
            else:
                result.new += 1
                pending.append(item)
                pending_ids.add(item.id)

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

    targets = pending[:ai_limit] if ai_limit is not None and not adaptive else pending
    if not adaptive and ai_limit is not None and len(pending) > ai_limit:
        log.info(
            "Monitor %s: AI stage capped, processing %d of %d pending items",
            monitor.id,
            ai_limit,
            len(pending),
        )
    matched_items: list[SearchItem] = []

    def can_process() -> bool:
        return db.rule_is_current(monitor) and (
            coverage is None or db.search_is_current(monitor.id, coverage.generation)
        )

    def can_judge(item: SearchItem) -> bool:
        return can_process() and not db.is_ignored(monitor.id, item.id) and (
            coverage is None or db.has_candidate(monitor.id, item.id)
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
            if notifier is not None:
                matched_items.append(item)
            log.info("MATCH: %s | %s | %s", item.id, item.title, _fmt_price(item.price))
        else:
            result.ai_rejected += 1

    try:
        if judge is None and monitor.ai_requirement:
            result.ai_errors += len(targets)
            pause_reason = ai_pause_reason()
        else:
            for item in targets:
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
                            if coverage is not None and item.id not in coverage.current_candidates:
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
                            if coverage is not None and not passes(item, rule):
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
                                    coverage.generation if coverage is not None else None,
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
                if adaptive and result.ai_errors > previous_errors and can_judge(item):
                    db.defer_candidate(
                        monitor.id, item.id, time(),
                        rule_version=monitor.rule_version,
                    )
    finally:
        if mail_batch is not None:
            outbox.finish_batch(mail_batch)

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
    if coverage is not None:
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
    if detail is None and generation is not None:
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
        if generation is not None:
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
