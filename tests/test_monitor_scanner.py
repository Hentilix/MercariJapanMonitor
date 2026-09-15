"""Offline tests for the Phase 5.3-A monitor-centric scanner.

Fake Mercari client + real SQLite in tmp_path. No network, no DeepSeek, no SMTP.
"""

import asyncio

import pytest

from app.database import Database
from app.mercari import ItemDetail, MercariError, SearchItem
from app.scanner import scan_monitor


class FakeClient:
    def __init__(self, items=None, error=None, details=None, detail_errors=None):
        self._items = items or []
        self._error = error
        self._details = details or {}
        self._detail_errors = detail_errors or {}
        self.detail_calls = []  # (item_id, item_type)

    async def search_products(self, query, **kwargs):
        if self._error is not None:
            raise self._error
        return list(self._items)

    async def get_item_details(self, item_id, item_type=None):
        self.detail_calls.append((item_id, item_type))
        if item_id in self._detail_errors:
            raise self._detail_errors[item_id]
        return self._details.get(item_id)


def make_item(
    item_id, title, price, item_type="ITEM_TYPE_MERCARI", published_at=None
):
    return SearchItem(
        id=item_id,
        title=title,
        price=price,
        url=f"https://jp.mercari.com/item/{item_id}",
        item_type=item_type,
        published_at=published_at,
    )


def make_detail(title="T", description="D", condition="C", category="K"):
    return ItemDetail(
        title=title, description=description, condition=condition, category=category
    )


def run(coro):
    return asyncio.run(coro)


def make_monitor(db, **overrides):
    fields = dict(
        name="Test Monitor",
        keywords="Built to Spill",
        keyword_mode="AND",
        min_price=None,
        max_price=None,
        filter_words="",
        ai_requirement="必须是 CD",
        interval_minutes=30,
        notification_email=None,
        enabled=True,
    )
    fields.update(overrides)
    monitor_id = db.create_monitor(**fields)
    return db.get_monitor(monitor_id)


def fake_judge(verdicts, calls):
    async def judge(detail):
        calls.append(detail)
        return verdicts[len(calls) - 1]

    return judge


def make_db(tmp_path, **monitor_overrides):
    db = Database(tmp_path / "test.db")
    return db, make_monitor(db, **monitor_overrides)


def make_notifier(batches):
    async def notifier(items):
        batches.append([i.id for i in items])

    return notifier


# ------------------------------------------------------- monitor independence
def test_two_monitors_process_same_item_independently(tmp_path):
    db = Database(tmp_path / "test.db")
    mon_a = make_monitor(db, name="A")
    mon_b = make_monitor(db, name="B")
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls_a, calls_b = [], []
    run(
        scan_monitor(
            client, db, mon_a, judge=fake_judge([True], calls_a)
        )
    )
    run(
        scan_monitor(
            client, db, mon_b, judge=fake_judge([True], calls_b)
        )
    )
    assert len(calls_a) == 1  # A judged X
    assert len(calls_b) == 1  # B judged X independently
    assert db.has_monitor_product(mon_a.id, "X")
    assert db.has_monitor_product(mon_b.id, "X")
    db.close()


# -------------------------------------------------------------------- ignore
def test_ignored_item_is_skipped_only_for_that_monitor(tmp_path):
    db = Database(tmp_path / "test.db")
    mon_a = make_monitor(db, name="A")
    mon_b = make_monitor(db, name="B")
    db.ignore_product(mon_a.id, "X")
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls_a, calls_b = [], []
    result_a = run(
        scan_monitor(client, db, mon_a, judge=fake_judge([True], calls_a))
    )
    assert result_a.ignored == 1
    assert calls_a == []                 # no detail, no DeepSeek for A
    assert client.detail_calls == []     # A's scan fetched nothing

    result_b = run(
        scan_monitor(client, db, mon_b, judge=fake_judge([True], calls_b))
    )
    assert result_b.ai_matched == 1      # B processed it normally
    assert len(calls_b) == 1
    db.close()


# ------------------------------------------------------------ already seen
def test_processed_item_is_not_reprocessed(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls = []
    first = run(
        scan_monitor(client, db, mon, judge=fake_judge([True], calls))
    )
    assert first.ai_matched == 1
    second = run(
        scan_monitor(client, db, mon, judge=fake_judge([True, True], calls))
    )
    assert second.already_seen == 1
    assert len(calls) == 1               # judge ran only once (first scan)
    assert len(client.detail_calls) == 1  # full_item only once
    db.close()


# ------------------------------------------------------- DeepSeek false/error
def test_deepseek_false_is_processed_without_history(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls = []
    first = run(
        scan_monitor(client, db, mon, judge=fake_judge([False], calls))
    )
    assert first.ai_rejected == 1
    assert db.list_monitor_products(mon.id) == []   # NOT in history
    assert db.has_monitor_product(mon.id, "X")      # but processed

    second = run(
        scan_monitor(client, db, mon, judge=fake_judge([False, False], calls))
    )
    assert second.already_seen == 1
    assert len(calls) == 1               # DeepSeek never called again
    db.close()


def test_deepseek_error_is_retried_next_scan(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls = []
    first = run(
        scan_monitor(client, db, mon, judge=fake_judge([None], calls))
    )
    assert first.ai_errors == 1
    assert not db.has_monitor_product(mon.id, "X")  # NOT processed

    calls2 = []
    second = run(
        scan_monitor(client, db, mon, judge=fake_judge([True], calls2))
    )
    assert second.new == 1               # new again
    assert second.ai_matched == 1        # and it succeeded this time
    assert db.has_monitor_product(mon.id, "X")
    db.close()


def test_full_item_error_is_retried_next_scan(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        detail_errors={"X": MercariError("Mercari item fetch failed: boom")},
    )
    calls = []
    first = run(
        scan_monitor(client, db, mon, judge=fake_judge([True], calls))
    )
    assert first.ai_errors == 1
    assert not db.has_monitor_product(mon.id, "X")

    # second scan: detail now available
    client2 = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls2 = []
    second = run(
        scan_monitor(client2, db, mon, judge=fake_judge([True], calls2))
    )
    assert second.ai_matched == 1
    assert db.has_monitor_product(mon.id, "X")
    db.close()


# --------------------------------------------------------------------- SMTP
def test_smtp_failure_does_not_undo_processed_state(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls = []

    async def notifier(items):
        raise RuntimeError("smtp down")

    result = run(
        scan_monitor(
            client, db, mon,
            judge=fake_judge([True], calls),
            notifier=notifier,
        )
    )
    assert result.ai_matched == 1
    assert result.ai_errors == 0
    assert db.has_monitor_product(mon.id, "X")  # saved despite email failure
    db.close()


def test_batch_email_sent_once_with_all_matches(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item(i, f"Built to Spill {i} CD", 1200) for i in ("1", "2", "3")],
        details={i: make_detail() for i in ("1", "2", "3")},
    )
    calls = []
    batches = []
    result = run(
        scan_monitor(
            client, db, mon,
            judge=fake_judge([True, True, True], calls),
            notifier=make_notifier(batches),
        )
    )
    assert result.ai_matched == 3
    assert batches == [["1", "2", "3"]]  # ONE batch with all three
    db.close()


def test_zero_matches_never_notifies(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )
    calls = []
    batches = []
    run(
        scan_monitor(
            client, db, mon,
            judge=fake_judge([False], calls),
            notifier=make_notifier(batches),
        )
    )
    assert batches == []
    db.close()


# ------------------------------------------------------------------ filters
def test_and_mode_requires_all_keywords(tmp_path):
    db, mon = make_db(tmp_path, keywords="Built to Spill，CD", keyword_mode="AND")
    client = FakeClient(
        [
            make_item("1", "Built to Spill Keep It Like a Secret CD", 1200),
            make_item("2", "Built to Spill Keep It Like a Secret LP", 1200),
        ]
    )
    result = run(scan_monitor(client, db, mon))
    assert result.candidates == 1  # only the CD title passes Level 1
    assert db.has_monitor_product(mon.id, "1")
    assert not db.has_monitor_product(mon.id, "2")
    db.close()


def test_or_mode_requires_any_keyword(tmp_path):
    db, mon = make_db(tmp_path, keywords="Built to Spill，Radiohead", keyword_mode="OR")
    client = FakeClient(
        [
            make_item("1", "Built to Spill CD", 1200),
            make_item("2", "Radiohead OK Computer", 1200),
            make_item("3", "Nirvana Nevermind", 1200),
        ]
    )
    result = run(scan_monitor(client, db, mon))
    assert result.candidates == 2
    db.close()


def test_filter_words_case_insensitive(tmp_path):
    db, mon = make_db(tmp_path, filter_words="LP, Vinyl")
    client = FakeClient(
        [
            make_item("1", "Built to Spill vinyl LP", 1200),
            make_item("2", "Built to Spill VINYL record", 1200),
            make_item("3", "Built to Spill lp", 1200),
            make_item("4", "Built to Spill CD", 1200),
        ]
    )
    result = run(scan_monitor(client, db, mon))
    assert result.candidates == 1  # only the CD survives
    assert db.has_monitor_product(mon.id, "4")
    db.close()


def test_price_bounds(tmp_path):
    db, mon = make_db(tmp_path, min_price=1000, max_price=5000)
    client = FakeClient(
        [
            make_item("1", "Built to Spill 1", 999),
            make_item("2", "Built to Spill 2", 1000),   # boundary, included
            make_item("3", "Built to Spill 3", 5000),   # boundary, included
            make_item("4", "Built to Spill 4", 5001),
            make_item("5", "Built to Spill 5", None),   # no price -> fails
        ]
    )
    result = run(scan_monitor(client, db, mon))
    assert result.candidates == 2
    assert db.has_monitor_product(mon.id, "2")
    assert db.has_monitor_product(mon.id, "3")
    db.close()


def test_price_only_min_or_only_max(tmp_path):
    db, mon_min = make_db(tmp_path, name="min only", min_price=3000)
    client = FakeClient(
        [make_item("lo", "Built to Spill lo", 1500), make_item("hi", "Built to Spill hi", 3000)]
    )
    result = run(scan_monitor(client, db, mon_min))
    assert result.candidates == 1
    assert db.has_monitor_product(mon_min.id, "hi")

    db2, mon_max = make_db(tmp_path, name="max only", max_price=2000)
    result2 = run(scan_monitor(client, db2, mon_max))
    assert result2.candidates == 1
    assert db2.has_monitor_product(mon_max.id, "lo")
    db.close()
    db2.close()


# ------------------------------------------------------------- Mercari Shops
def test_beyond_item_passes_item_type_to_detail_fetch(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("2JWGvFpEzgTBTpivj2GB9A", "Built to Spill Shop CD", 1194,
                   item_type="ITEM_TYPE_BEYOND")],
        details={"2JWGvFpEzgTBTpivj2GB9A": make_detail()},
    )
    calls = []
    run(scan_monitor(client, db, mon, judge=fake_judge([True], calls)))
    assert client.detail_calls == [
        ("2JWGvFpEzgTBTpivj2GB9A", "ITEM_TYPE_BEYOND")
    ]  # scanner forwards item_type -> MercariClient routes to m.product()
    db.close()


# ------------------------------------------------------- published_at + scan
def test_published_at_is_saved(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200,
                   published_at="2026-09-15 18:32:41")],
        details={"X": make_detail()},
    )
    calls = []
    run(scan_monitor(client, db, mon, judge=fake_judge([True], calls)))
    row = db.list_monitor_products(mon.id)[0]
    assert row[4] == "2026-09-15 18:32:41"  # published_at from search result
    assert row[5] is not None               # found_at auto-generated
    db.close()


def test_last_scan_result_is_updated(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [
            make_item("1", "Built to Spill 1 CD", 1200),
            make_item("2", "Built to Spill 2 CD", 1200),
        ],
        details={"1": make_detail(), "2": make_detail()},
    )
    calls = []
    run(scan_monitor(client, db, mon, judge=fake_judge([True, False], calls)))
    mon = db.get_monitor(mon.id)
    assert mon.last_scan_at is not None
    assert "found=2" in mon.last_result
    assert "matched=1" in mon.last_result
    assert "rejected=1" in mon.last_result
    db.close()


def test_search_failure_records_status_and_raises(tmp_path):
    db, mon = make_db(tmp_path)
    client = FakeClient(error=MercariError("Mercari search failed: boom"))
    with pytest.raises(MercariError):
        run(scan_monitor(client, db, mon, judge=None))
    mon = db.get_monitor(mon.id)
    assert mon.last_result.startswith("搜索失败")
    db.close()


def test_no_ai_gate_records_all_candidates(tmp_path):
    """Monitor without ai_requirement/judge: Level 1 pass == matched."""
    db, mon = make_db(tmp_path, ai_requirement="")
    client = FakeClient(
        [
            make_item("1", "Built to Spill 1 CD", 1200),
            make_item("2", "Built to Spill 2 CD", 1200),
        ]
    )
    result = run(scan_monitor(client, db, mon))
    assert result.ai_matched == 2
    assert len(db.list_monitor_products(mon.id)) == 2
    db.close()


def test_judge_crash_is_not_processed(tmp_path):
    """A crashing judge counts as an AI error; the item is retried next scan."""
    db, mon = make_db(tmp_path)
    client = FakeClient(
        [make_item("X", "Built to Spill X CD", 1200)],
        details={"X": make_detail()},
    )

    async def judge(detail):
        raise RuntimeError("judge crashed")

    first = run(scan_monitor(client, db, mon, judge=judge))
    assert first.ai_errors == 1
    assert not db.has_monitor_product(mon.id, "X")  # NOT processed

    calls = []
    second = run(
        scan_monitor(client, db, mon, judge=fake_judge([True], calls))
    )
    assert second.new == 1
    assert second.ai_matched == 1
    assert db.has_monitor_product(mon.id, "X")
    db.close()


def test_ai_limit_leaves_rest_unprocessed(tmp_path):
    """Items beyond ai_limit are not processed and therefore not recorded."""
    db, mon = make_db(tmp_path)
    ids = [str(i) for i in range(3)]
    client = FakeClient(
        [make_item(i, f"Built to Spill {i} CD", 1200) for i in ids],
        details={i: make_detail() for i in ids},
    )
    calls = []
    result = run(
        scan_monitor(
            client, db, mon,
            judge=fake_judge([True, True, True], calls),
            ai_limit=2,
        )
    )
    assert result.ai_matched == 2
    assert db.has_monitor_product(mon.id, "0")
    assert db.has_monitor_product(mon.id, "1")
    assert not db.has_monitor_product(mon.id, "2")  # beyond the limit
    db.close()
