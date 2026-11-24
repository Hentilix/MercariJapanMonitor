"""Bounded newest-first search with durable per-monitor pagination."""

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import time

from app.database import Database, Monitor
from app.filter import FilterRule, passes
from app.mercari import InvalidPageToken, MercariClient, MercariError, SearchItem
from app.request_budget import RequestDeferred, guard_requests

log = logging.getLogger(__name__)
FIRST_PAGES = 20
ROUND_PAGES = 6
FRONT_PAGES = 3
AI_PER_ROUND = 50


@dataclass
class Coverage:
    generation: str
    current_candidates: set[str] = field(default_factory=set)
    first: int = 0
    front: int = 0
    deep: int = 0
    ends: int = 0
    found: int = 0
    candidates: int = 0
    queued: int = 0
    seen: int = 0
    ignored: int = 0
    missing_dates: int = 0
    failures: int = 0
    resets: int = 0
    limited: bool = False

    def summary(self) -> str:
        text = (f"首扫 {self.first} 页 / 追新 {self.front} 页 / 补扫 {self.deep} 页"
                f" · 到达末页 {self.ends} 次")
        if self.limited:
            text += " · 本轮翻页预算用完"
        if self.resets:
            text += f" · 令牌失效或循环，重置 {self.resets} 次"
        if self.failures:
            text += f" · 搜索失败 {self.failures} 次，下轮继续"
        if self.missing_dates:
            text += f" · 补扫缺发布时间 {self.missing_dates} 件"
        return text


async def discover(client: MercariClient, db: Database, monitor: Monitor,
                   rule: FilterRule, *, clock=time) -> Coverage | None:
    queries = (list(dict.fromkeys(rule.keywords)) if rule.match_mode == "OR"
               else [" ".join(rule.keywords)])
    initial = {
        "since": int(clock()), "first_pages": 0, "initial_done": False,
        "initial_order": queries, "initial_cursor": {},
        "front_order": queries, "deep_order": queries, "deep_cursor": {},
    }
    saved = db.begin_search(monitor, initial)
    if saved is None:
        return None
    generation, state, created = saved
    result = Coverage(generation)
    if not queries:
        return result
    initial_pending = not state["initial_done"]
    resume_with_front = initial_pending and not created
    budget = (FIRST_PAGES - state["first_pages"]
              if initial_pending and not resume_with_front else ROUND_PAGES)
    blocked = set()
    requested = set()
    found_ids = set()
    candidate_ids = set()
    ignored_ids = set()
    seen_ids = set()
    front_paths = {query: [] for query in queries}
    front_next = dict.fromkeys(queries)

    def save_page(candidates: list[tuple], *, refresh=(), discard=()) -> int | None:
        return db.save_search_page(
            monitor.id, generation, state, candidates, rule_version=monitor.rule_version,
            queued_at=clock(), refresh=refresh, discard=discard,
        )

    def search_is_current() -> bool:
        return db.rule_is_current(monitor) and db.search_is_current(monitor.id, generation)

    def process_page(items: list[SearchItem], phase: str) -> bool:
        candidates = []
        refresh = []
        discard = []
        items_by_id = {item.id: item for item in items}
        statuses = db.page_item_statuses(monitor.id, items_by_id)
        for item_id, item in items_by_id.items():
            status = statuses[item_id]
            pending = status.pending
            decision = status.decision
            needs_judgment = monitor.needs_judgment(decision)
            eligible = (pending or phase == "first" or (decision is not None and needs_judgment) or
                        (item.created_epoch is not None and item.created_epoch >= state["since"]) or
                        status.recheck)
            if not eligible and item.created_epoch is None:
                if phase == "deep":
                    result.missing_dates += 1
                    continue
                eligible = True
            found_ids.add(item.id)
            if not eligible:
                continue
            if not passes(item, rule):
                if pending:
                    discard.append(item.id)
                continue
            candidate_ids.add(item.id)
            if status.ignored:
                ignored_ids.add(item.id)
            elif not needs_judgment:
                seen_ids.add(item.id)
            elif pending:
                refresh.append((item.id, item.title, item.price))
            else:
                candidates.append((item.id, item.title, item.price, item.url, item.item_type,
                                   item.published_at, item.created_epoch))
        inserted = save_page(candidates, refresh=refresh, discard=discard)
        if inserted is None:
            return False
        result.current_candidates.difference_update(discard)
        result.current_candidates.update(row[0] for row in candidates)
        result.current_candidates.update(row[0] for row in refresh)
        result.queued += inserted
        result.candidates = len(candidate_ids)
        result.found = len(found_ids)
        result.ignored = len(ignored_ids)
        result.seen = len(seen_ids)
        return True

    async def fetch(query, token, trail, phase, update):
        nonlocal budget
        budget -= 1
        requested.add((query, token))
        try:
            with guard_requests(search_is_current):
                page = await client.search_page(
                    query, page_token=token, min_price=monitor.min_price,
                    max_price=monitor.max_price, exclude=" ".join(rule.exclude_keywords) or None,
                )
        except RequestDeferred:
            return False
        except InvalidPageToken:
            result.resets += 1
            if not resume_with_front or phase == "first":
                blocked.add(query)
            update(None, [], False)
            return save_page([]) is not None
        except MercariError as exc:
            result.failures += 1
            if not resume_with_front:
                blocked.add(query)
            log.warning("Monitor %s query %r page failed: %s", monitor.id, query, exc)
            return save_page([]) is not None

        setattr(result, phase, getattr(result, phase) + 1)
        trail = [*trail, token]
        following = page.next_page_token or None
        cycle = following is not None and following in trail
        if cycle:
            result.resets += 1
            blocked.add(query)
        elif following is None:
            result.ends += 1
        update(None if cycle else following, [] if cycle else trail, following is None)

        return process_page(page.items, phase)

    async def run_actions(
        actions: tuple[tuple[Callable[[], Awaitable[bool | None]], int], ...],
    ) -> bool:
        """Spend the shared page budget in priority order."""
        for action, count in actions:
            for _ in range(count):
                if not budget:
                    return True
                advanced = await action()
                if advanced is False:
                    return False
                if advanced is None:
                    break
        return True

    if initial_pending:
        order = deque(state["initial_order"])
        front_order = deque(state["front_order"])

        async def fetch_first(query, token, trail):
            before = result.first

            def save_first(following, visited, ended):
                state["first_pages"] += result.first - before
                if ended:
                    order.remove(query)
                    state["initial_order"] = list(order)
                    state["initial_cursor"].pop(query, None)
                else:
                    state["initial_cursor"][query] = [following, visited]
                state["initial_done"] = not order or state["first_pages"] >= FIRST_PAGES

            return await fetch(query, token, trail, "first", save_first)

        async def continue_first():
            if state["initial_done"]:
                return None
            for _ in range(len(order)):
                query = order[0]
                order.rotate(-1)
                token, trail = state["initial_cursor"].get(query, (None, []))
                if query in blocked or (query, token) in requested:
                    continue
                state["initial_order"] = list(order)
                return await fetch_first(query, token, trail)
            return None

        async def check_homepage():
            for _ in range(len(front_order)):
                query = front_order[0]
                front_order.rotate(-1)
                if (query, None) in requested:
                    continue
                state["front_order"] = list(front_order)
                token, trail = state["initial_cursor"].get(query, (None, []))
                if not state["initial_done"] and query in order and token is None and query not in blocked:
                    return await fetch_first(query, token, trail)
                return await fetch(query, None, [], "front", lambda *_: None)
            return None

        actions = (
            ((check_homepage, FRONT_PAGES), (continue_first, ROUND_PAGES - FRONT_PAGES),
             (check_homepage, ROUND_PAGES), (continue_first, ROUND_PAGES))
            if resume_with_front else ((continue_first, budget),)
        )
        if not await run_actions(actions):
            return None
        result.limited = budget == 0 and bool(order)
        return result

    front_order = deque(state["front_order"])
    deep_order = deque(state["deep_order"])
    front_done = set()
    deep_done = set()

    async def front():
        for _ in range(len(front_order)):
            query = front_order[0]
            front_order.rotate(-1)
            if query in front_done or query in blocked:
                continue
            if (query, front_next[query]) in requested:
                front_done.add(query)
                continue
            state["front_order"] = list(front_order)

            def update(following, visited, ended):
                front_paths[query] = visited
                front_next[query] = following
                if ended:
                    front_done.add(query)
                    deep_done.add(query)
                    state["deep_cursor"].pop(query, None)
                elif not visited:
                    state["deep_cursor"].pop(query, None)

            return await fetch(query, front_next[query], front_paths[query], "front", update)
        return None

    async def deep():
        for _ in range(len(deep_order)):
            query = deep_order[0]
            deep_order.rotate(-1)
            if query in deep_done or query in blocked:
                continue
            token, trail = state["deep_cursor"].get(query, (None, []))
            if (query, token) in requested:
                if token not in front_paths[query]:
                    continue
                if query in front_done:
                    deep_done.add(query)
                    state["deep_cursor"].pop(query, None)
                    continue
                token = front_next[query]
                trail = front_paths[query]
            state["deep_order"] = list(deep_order)

            def update(following, visited, ended):
                if ended or not visited:
                    state["deep_cursor"].pop(query, None)
                    deep_done.add(query)
                else:
                    state["deep_cursor"][query] = [following, visited]

            return await fetch(query, token, trail, "deep", update)
        return None

    if not await run_actions((
        (front, FRONT_PAGES), (deep, ROUND_PAGES - FRONT_PAGES),
        (front, ROUND_PAGES), (deep, ROUND_PAGES),
    )):
        return None
    if save_page([]) is None:
        return None
    result.limited = budget == 0 and any(
        query not in deep_done or query not in front_done for query in queries
    )
    return result
