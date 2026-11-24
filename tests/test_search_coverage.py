import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.coverage import discover
from app.database import Database
from app.filter import FilterRule
from app.mercari import InvalidPageToken, ItemDetail, MercariError, SearchItem, SearchPage
from app.scanner import scan_monitor


class Pages:
    def __init__(self, count, fail_at=None):
        self.count = count
        self.fail_at = fail_at
        self.calls = []

    async def search_page(self, query, *, page_token, **kwargs):
        page = int(page_token or 1)
        self.calls.append((query, page))
        if page == self.fail_at:
            raise MercariError("offline")
        return SimpleNamespace(
            items=[SearchItem(f"{query}-{page}", f"CD {query}", 100, "https://example.test")],
            next_page_token=str(page + 1) if page < self.count else None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("changes", "max_price", "discarded"), [
    ({"price": 200}, 150, True),
    ({"title": "CD broken"}, 150, True),
    ({"price": None}, 150, True),
    ({"price": None}, None, False),
])
async def test_rediscovered_candidate_is_filtered_again(changes, max_price, discarded, monkeypatch):
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(
        name="CD", keywords="CD", ai_requirement="CD", max_price=max_price, filter_words="broken",
    ))
    monkeypatch.setattr("app.scanner.time", lambda: 1000)
    original = SearchItem("A", "CD album", 100, "url", created_epoch=900)
    client = AsyncMock()
    client.get_item_details.return_value = ItemDetail(original.title, None, None, None)
    judge = AsyncMock(return_value=True)
    try:
        client.search_page.return_value = SearchPage([original], None)
        await scan_monitor(client, db, monitor)
        client.search_page.return_value = SearchPage([replace(original, **changes)], None)
        result = await scan_monitor(client, db, monitor, judge=judge)
        assert result.new == 0
        if discarded:
            judge.assert_not_awaited()
            assert db.pending_candidates(monitor.id) == 0
            assert db.wants_recheck(monitor.id, "A")
            client.search_page.return_value = SearchPage([original], None)
            result = await scan_monitor(client, db, monitor, judge=judge)
            assert result.new == result.ai_matched == 1
            assert not db.wants_recheck(monitor.id, "A")
        else:
            assert result.ai_matched == 1
            assert db.list_monitor_products(monitor.id)[0][2] is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_later_or_page_refreshes_title_and_invalidates_cached_detail(monkeypatch):
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(
        name="AB", keywords="A,B", keyword_mode="OR", ai_requirement="album",
    ))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)
    original = SearchItem("AB", "AB album", 100, "url", created_epoch=900)
    edited = replace(original, title="AB reissue", price=200)
    client = AsyncMock()

    async def search(query, **kwargs):
        return SearchPage([edited if now > 1000 and query == "B" else original], None)

    client.search_page.side_effect = search
    client.get_item_details.side_effect = [
        ItemDetail(original.title, None, None, None),
        ItemDetail(edited.title, None, None, None),
    ]
    judge = AsyncMock(return_value=None)
    try:
        await scan_monitor(client, db, monitor, judge=judge)
        now = 1300
        judge.return_value = True
        result = await scan_monitor(client, db, monitor, judge=judge)
        assert result.new == 0
        assert result.ai_matched == 1
        assert judge.await_args.args[0].title == edited.title
        assert db.list_monitor_products(monitor.id)[0][1:3] == (edited.title, 200)
    finally:
        db.close()


def test_candidate_changes_and_progress_roll_back_together():
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    generation, _, _ = db.begin_search(monitor, {"page": 1})
    existing = [("A", "CD", 100, "url", None, None, None),
                ("B", "CD", 100, "url", None, None, None)]
    try:
        db.save_search_page(monitor.id, generation, {"page": 1}, existing,
                            rule_version=monitor.rule_version, queued_at=1000)
        db._conn.execute("""
            CREATE TRIGGER reject_insert BEFORE INSERT ON scan_candidates
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            db.save_search_page(
                monitor.id, generation, {"page": 2},
                [("C", "CD", 100, "url", None, None, None)],
                rule_version=monitor.rule_version, queued_at=1100,
                refresh=[("A", "CD", 200)], discard=["B"],
            )
        assert db.begin_search(monitor, {})[1] == {"page": 1}
        assert db.candidate_rows(monitor.id, 1100, None) == existing
        assert not db.wants_recheck(monitor.id, "B")
        assert db.save_search_page(
            monitor.id, generation, {"page": 2}, [],
            rule_version=monitor.rule_version - 1, queued_at=1100,
            refresh=[("A", "CD", 200)], discard=["B"],
        ) is None
        assert db.begin_search(monitor, {})[1] == {"page": 1}
        assert db.candidate_rows(monitor.id, 1100, None) == existing
        assert not db.wants_recheck(monitor.id, "B")
    finally:
        db.close()


@pytest.mark.parametrize(("total", "expected"), [(4, 4), (25, 20)])
def test_first_scan_budget(tmp_path, total, expected):
    db = Database(tmp_path / "scan.db")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    pages = Pages(total)
    result = asyncio.run(scan_monitor(pages, db, monitor))
    assert len(pages.calls) == result.ai_matched == expected
    db.close()


def test_restart_resumes_first_scan(tmp_path):
    path = tmp_path / "scan.db"
    db = Database(path)
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    first = Pages(4, fail_at=3)
    result = asyncio.run(scan_monitor(first, db, monitor))
    assert result.ai_matched == 2
    db.close()

    db = Database(path)
    second = Pages(4)
    result = asyncio.run(scan_monitor(second, db, monitor))
    assert second.calls == [("CD", 1), ("CD", 3), ("CD", 4)]
    assert result.ai_matched == 2
    assert db.count_monitor_products(monitor.id) == 4
    assert db.begin_search(monitor, {})[1]["initial_done"]
    db.close()


@pytest.mark.parametrize("keywords", ["CD", "CD,album"])
def test_single_query_checks_homepage_while_first_scan_page_keeps_failing(keywords, monkeypatch):
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords=keywords))
    query = keywords.replace(",", " ")
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)

    class Listings(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            page = await super().search_page(query, page_token=page_token, **kwargs)
            page.items = [SearchItem(f"item-{now}", query, 100, "url", created_epoch=now)]
            return page

    try:
        for now in (1000, 2000, 3000):
            pages = Listings(3, fail_at=2)
            result = asyncio.run(scan_monitor(pages, db, monitor))
            assert pages.calls == [(query, 1), (query, 2)]
            assert result.ai_matched == 1
            assert db.has_monitor_product(monitor.id, f"item-{now}")
            state = db.begin_search(monitor, {})[1]
            assert state["since"] == 1000
            assert state["first_pages"] == 1
            assert state["initial_cursor"][query][0] == "2"

        pages = Pages(3, fail_at=1)
        asyncio.run(scan_monitor(pages, db, monitor))
        assert pages.calls == [(query, 1), (query, 2), (query, 3)]
        assert db.begin_search(monitor, {})[1]["initial_done"]
        assert db.count_monitor_products(monitor.id) == 5
    finally:
        db.close()


def test_single_query_first_scan_resume_respects_both_page_budgets():
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    try:
        asyncio.run(scan_monitor(Pages(30, fail_at=13), db, monitor))
        for expected_pages, total in (([1, 13, 14, 15, 16, 17], 17), ([1, 18, 19, 20], 20)):
            pages = Pages(30)
            asyncio.run(scan_monitor(pages, db, monitor))
            assert [page for _, page in pages.calls] == expected_pages
            state = db.begin_search(monitor, {})[1]
            assert state["first_pages"] == total
            assert state["initial_done"] == (total == 20)
    finally:
        db.close()


def test_or_keeps_checking_both_homepages_during_a_persistent_page_failure(tmp_path, monkeypatch):
    path = tmp_path / "scan.db"
    db = Database(path)
    monitor = db.get_monitor(db.create_monitor(name="AB", keywords="A,B", keyword_mode="OR"))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)

    class Listings(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            self.calls.append((query, page_token))
            if page_token == "2" and now < 4000:
                raise MercariError("B page 2 unavailable")
            item_id = "shared-old" if page_token else f"{query}-{now}"
            items = [SearchItem(item_id, "AB", 100, "url",
                                created_epoch=999 if page_token else now)]
            if query == "A" and now > 1000:
                items.append(SearchItem("shared-old", "AB", 100, "url", created_epoch=999))
            token = "2" if query == "B" and page_token is None else None
            return SimpleNamespace(items=items, next_page_token=token)

    def scan():
        listings = Listings(1)
        asyncio.run(scan_monitor(listings, db, monitor))
        return listings.calls

    try:
        assert scan() == [("A", None), ("B", None), ("B", "2")]
        for now in (2000, 3000, 4000):
            db.close()
            db = Database(path)
            assert scan() == [("A", None), ("B", None), ("B", "2")]
            assert db.has_monitor_product(monitor.id, f"A-{now}")
            assert db.has_monitor_product(monitor.id, f"B-{now}")
            assert db.has_monitor_product(monitor.id, "A-1000")
            assert db.has_monitor_product(monitor.id, "shared-old") == (now == 4000)
            assert db.begin_search(monitor, {})[1]["since"] == 1000
    finally:
        db.close()


@pytest.mark.parametrize("saved_progress", [False, True])
def test_or_incomplete_first_scan_rotates_within_six_page_budget(tmp_path, saved_progress):
    db = Database(tmp_path / "scan.db")
    words = [f"CD{i}" for i in range(8)]
    monitor = db.get_monitor(db.create_monitor(
        name="OR", keywords=",".join(words), keyword_mode="OR",
    ))

    class Listings(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            self.calls.append((query, page_token))
            if query == "CD0" or page_token is not None:
                raise MercariError("page unavailable")
            return SimpleNamespace(items=[], next_page_token="2")

    try:
        if saved_progress:
            # Old databases could have saved progress even if every homepage failed.
            db.begin_search(monitor, {
                "since": 1000, "first_pages": 0, "initial_done": False,
                "initial_order": words, "initial_cursor": {},
                "front_order": words, "deep_order": words, "deep_cursor": {},
            })
        first = Listings(1)
        asyncio.run(scan_monitor(first, db, monitor))
        assert len(first.calls) == len(set(first.calls)) == (6 if saved_progress else 15)
        homes = set()
        retries = set()
        for _ in range(6):
            pages = Listings(1)
            asyncio.run(scan_monitor(pages, db, monitor))
            assert len(pages.calls) == len(set(pages.calls)) == 6
            homes.update(query for query, token in pages.calls if token is None)
            retries.update(query for query, token in pages.calls if token == "2")
        assert homes == set(words)
        assert retries == set(words[1:])
    finally:
        db.close()


def test_or_rotates_beyond_first_twenty_queries(tmp_path):
    db = Database(tmp_path / "scan.db")
    words = ["CD", *(f"CD{i}" for i in range(24))]
    monitor = db.get_monitor(db.create_monitor(
        name="OR", keywords=",".join(words), keyword_mode="OR",
    ))
    first = Pages(4)
    asyncio.run(scan_monitor(first, db, monitor))
    assert first.calls == [(word, 1) for word in words[:20]]
    searched = set(first.calls)
    for _ in range(10):
        pages = Pages(4)
        asyncio.run(scan_monitor(pages, db, monitor))
        assert len(set(pages.calls)) == len(pages.calls) <= 6
        searched.update(pages.calls)
    assert {(word, 1) for word in words} <= searched
    db.close()


def test_depth_keeps_advancing_past_old_pages_and_resets_invalid_token(tmp_path):
    db = Database(tmp_path / "scan.db")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    asyncio.run(scan_monitor(Pages(1), db, monitor))

    class MixedPages(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            page = await super().search_page(query, page_token=page_token, **kwargs)
            number = self.calls[-1][1]
            for listing in page.items:
                listing.created_epoch = 4_000_000_000 if number == 10 else 1
            return page

    first = MixedPages(12)
    asyncio.run(scan_monitor(first, db, monitor))
    assert [page for _, page in first.calls] == list(range(1, 7))
    second = MixedPages(12)
    asyncio.run(scan_monitor(second, db, monitor))
    assert [page for _, page in second.calls] == [1, 2, 3, 7, 8, 9]
    third = MixedPages(12)
    asyncio.run(scan_monitor(third, db, monitor))
    assert [page for _, page in third.calls] == [1, 2, 3, 10, 11, 12]
    assert db.has_monitor_product(monitor.id, "CD-10")
    assert not db.has_monitor_product(monitor.id, "CD-9")

    asyncio.run(scan_monitor(MixedPages(12), db, monitor))

    class Expired(MixedPages):
        async def search_page(self, query, *, page_token, **kwargs):
            if page_token == "7":
                raise InvalidPageToken("expired")
            return await super().search_page(query, page_token=page_token, **kwargs)

    asyncio.run(scan_monitor(Expired(12), db, monitor))
    assert "重置 1 次" in db.get_monitor(monitor.id).last_result
    restarted = MixedPages(12)
    asyncio.run(scan_monitor(restarted, db, monitor))
    assert [page for _, page in restarted.calls] == list(range(1, 7))
    db.close()


def test_ai_backlog_survives_restart_and_search_failure(tmp_path):
    path = tmp_path / "scan.db"
    db = Database(path)
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD", ai_requirement="CD"))

    class Batch(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            return SimpleNamespace(items=[
                SearchItem(str(i), "CD", 100, "https://example.test", created_epoch=i)
                for i in range(120)
            ], next_page_token=None)

        async def get_item_details(self, item_id, item_type):
            return ItemDetail("CD", item_id, None, None)

    checked = []

    async def judge(detail):
        checked.append(int(detail.description))
        return True

    asyncio.run(scan_monitor(Batch(1), db, monitor, judge=judge))
    assert checked == [*range(119, 94, -1), *range(25)]
    assert db.pending_candidates(monitor.id) == 70
    db.close()

    class Offline(Batch):
        async def search_page(self, *args, **kwargs):
            raise MercariError("offline")

        async def get_current_listing(self, item_id, item_type):
            return 100, await self.get_item_details(item_id, item_type)

    db = Database(path)
    asyncio.run(scan_monitor(Offline(1), db, monitor, judge=judge))
    assert len(set(checked)) == 100
    assert db.pending_candidates(monitor.id) == 20
    db.close()


def test_failing_candidates_do_not_starve_middle_item(tmp_path, monkeypatch):
    db = Database(tmp_path / "scan.db")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD", ai_requirement="CD"))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)
    attempted = []

    class Listings(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            return SimpleNamespace(items=[
                SearchItem(str(i), "CD", 100, "url", created_epoch=1000 + i)
                for i in range(51)
            ], next_page_token=None)

        async def get_item_details(self, item_id, item_type):
            attempted.append(item_id)
            if item_id == "25":
                return ItemDetail("CD", None, None, None)
            return None

    async def judge(detail):
        return True

    try:
        asyncio.run(scan_monitor(Listings(1), db, monitor, judge=judge))
        assert len(attempted) == 50
        assert "25" not in attempted
        attempted.clear()
        now += 1800
        asyncio.run(scan_monitor(Listings(1), db, monitor, judge=judge))
        assert db.product_decision(monitor.id, "25") == (1, 1)
        assert len(attempted) == len(set(attempted)) == 50
    finally:
        db.close()


def test_due_retries_and_waiting_items_get_slots_during_new_arrivals():
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    generation, state, _ = db.begin_search(monitor, {})

    def enqueue(ids, now):
        rows = [(str(i), "CD", 100, "url", None, None, i) for i in ids]
        db.save_search_page(
            monitor.id, generation, state, rows, rule_version=monitor.rule_version,
            queued_at=now,
        )

    def selected(now, limit):
        return [row[0] for row in db.candidate_rows(monitor.id, now, limit)]

    try:
        enqueue([1, 2, 3], 1000)
        db.defer_candidate(monitor.id, "1", 1000, rule_version=monitor.rule_version)
        assert selected(1299, 1) == ["2"]
        enqueue(range(4, 65), 1301)
        enqueue([1, 2], 1301)  # Rediscovery keeps the original eligibility times.
        assert selected(1301, 0) == []
        assert selected(1301, 1) == ["2"]
        assert selected(1301, 4) == ["64", "63", "2", "3"]
        for item_id in ("64", "63", "2", "3"):
            assert db.add_monitor_product(
                monitor.id, mercari_id=item_id, title="CD", price=100, url="url",
                rule_version=monitor.rule_version,
            )
        enqueue(range(65, 125), 1302)
        assert selected(1302, 1) == ["1"]
        assert selected(1302, 4) == ["124", "123", "1", "4"]
    finally:
        db.close()


def test_candidate_retry_backoff_and_detail_survive_restart(tmp_path, monkeypatch):
    path = tmp_path / "scan.db"
    db = Database(path)
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD", ai_requirement="CD"))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)
    detail_requests = []

    class Listing(Pages):
        async def get_item_details(self, item_id, item_type):
            detail_requests.append(item_id)
            return ItemDetail("CD", "original", None, None)

    def scan():
        return asyncio.run(scan_monitor(Listing(1), db, monitor, judge=judge))

    verdict = None

    async def judge(detail):
        assert detail.description == "original"
        return verdict

    try:
        for delay in (300, 900, 3600, 21600, 21600):
            assert scan().ai_errors == 1
            now += delay - 1
            db.close()
            db = Database(path)
            assert scan().ai_errors == 0
            assert db.pending_candidates(monitor.id) == 1
            now += 1
        verdict = True
        assert scan().ai_matched == 1
        assert db.pending_candidates(monitor.id) == 0
        assert detail_requests == ["CD-1"]
    finally:
        db.close()


def test_failed_candidate_insert_rolls_back_page_progress(tmp_path):
    path = tmp_path / "scan.db"
    db = Database(path)
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    db._conn.execute("""
        CREATE TRIGGER fail_page BEFORE INSERT ON scan_candidates
        WHEN new.mercari_id = 'CD-2'
        BEGIN SELECT RAISE(ABORT, 'insert failed'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="insert failed"):
        asyncio.run(scan_monitor(Pages(3), db, monitor))
    assert db.pending_candidates(monitor.id) == 1
    db._conn.execute("DROP TRIGGER fail_page")
    db.close()
    db = Database(path)
    pages = Pages(3)
    result = asyncio.run(scan_monitor(pages, db, monitor))
    assert pages.calls == [("CD", 1), ("CD", 2), ("CD", 3)]
    assert result.ai_matched == 3
    db.close()


def test_old_page_cannot_write_into_edited_search(tmp_path):
    db = Database(tmp_path / "scan.db")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))

    class EditDuringRequest(Pages):
        async def search_page(self, query, **kwargs):
            db.update_monitor(
                monitor.id, name="CD", keywords="LP", keyword_mode="AND",
                min_price=None, max_price=None, filter_words="", ai_requirement="",
                interval_minutes=30, notification_email=None, enabled=True,
            )
            return await super().search_page(query, **kwargs)

    asyncio.run(scan_monitor(EditDuringRequest(4), db, monitor))
    assert db.pending_candidates(monitor.id) == 0
    assert not db.has_monitor_product(monitor.id, "CD-1")
    pages = Pages(1)
    asyncio.run(scan_monitor(pages, db, db.get_monitor(monitor.id)))
    assert pages.calls == [("LP", 1)]
    db.close()


def test_deep_token_cycle_and_missing_dates_are_reported(tmp_path):
    db = Database(tmp_path / "scan.db")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
    asyncio.run(scan_monitor(Pages(1), db, monitor))

    class Loop(Pages):
        async def search_page(self, query, *, page_token, **kwargs):
            page = await super().search_page(query, page_token=page_token, **kwargs)
            if page_token == "4":
                page.next_page_token = "2"
            return page

    pages = Loop(10)
    asyncio.run(scan_monitor(pages, db, monitor))
    assert pages.calls == [("CD", n) for n in range(1, 5)]
    assert not db.has_monitor_product(monitor.id, "CD-4")
    summary = db.get_monitor(monitor.id).last_result
    assert "重置 1 次" in summary
    assert "补扫缺发布时间 1 件" in summary
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [1, 120])
async def test_page_status_lookup_uses_one_select(page_size):
    db = Database(":memory:")
    try:
        mon = db.get_monitor(db.create_monitor(name="CD", keywords="CD"))
        client = AsyncMock()
        client.search_page.return_value = SearchPage(
            [SearchItem(str(i), "CD", 100, "url") for i in range(page_size)], None,
        )
        reads = []
        db._conn.set_trace_callback(
            lambda sql: reads.append(sql) if sql.lstrip().upper().startswith(("SELECT", "WITH")) else None
        )
        result = await discover(client, db, mon, FilterRule(keywords=["CD"]))
        assert result.queued == page_size
        assert len(reads) == 2  # load progress, then all listing statuses
    finally:
        db.close()
