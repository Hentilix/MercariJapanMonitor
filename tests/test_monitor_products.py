"""Offline tests for Phase 5.2: monitor_products and ignored_products."""

from app.database import Database


def make_db(tmp_path, monitors=("A", "B")):
    db = Database(tmp_path / "test.db")
    ids = [db.create_monitor(name=name, keywords=name) for name in monitors]
    return db, ids


# ---------------------------------------------------------- monitor products
def test_add_and_list_monitor_product(tmp_path):
    db, (a_id, _b_id) = make_db(tmp_path)
    db.add_monitor_product(
        a_id, mercari_id="mX", title="商品 X", price=1200,
        url="https://jp.mercari.com/item/mX",
        published_at="2026-09-15 18:32:41",
    )
    rows = db.list_monitor_products(a_id)
    assert len(rows) == 1
    mercari_id, title, price, url, published_at, found_at = rows[0]
    assert mercari_id == "mX"
    assert title == "商品 X"
    assert price == 1200
    assert url == "https://jp.mercari.com/item/mX"
    assert published_at == "2026-09-15 18:32:41"
    assert found_at  # auto-generated ISO timestamp
    db.close()


def test_list_products_matched_filter(tmp_path):
    """matched=1 default / matched=0 rejected-only / matched=None both."""
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="m1", title="match", price=1, url="u1", matched=True)
    db.add_monitor_product(a_id, mercari_id="m2", title="reject", price=2, url="u2", matched=False)

    assert {r[0] for r in db.list_monitor_products(a_id)} == {"m1"}
    assert {r[0] for r in db.list_monitor_products(a_id, matched=0)} == {"m2"}
    assert {r[0] for r in db.list_monitor_products(a_id, matched=None)} == {"m1", "m2"}
    db.close()


def test_count_products_matched_filter(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="m1", title="match", price=1, url="u1", matched=True)
    db.add_monitor_product(a_id, mercari_id="m2", title="reject", price=2, url="u2", matched=False)

    assert db.count_monitor_products(a_id) == 1
    assert db.count_monitor_products(a_id, matched=0) == 1
    assert db.count_monitor_products(a_id, matched=None) == 2
    db.close()


def test_published_at_may_be_null(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(
        a_id, mercari_id="mX", title="T", price=None, url="https://jp.mercari.com/item/mX"
    )
    row = db.list_monitor_products(a_id)[0]
    assert row[1] == "T" and row[2] is None and row[4] is None  # title/price/published
    db.close()


def test_has_monitor_product(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    assert not db.has_monitor_product(a_id, "mX")
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u")
    assert db.has_monitor_product(a_id, "mX")
    db.close()


def test_same_monitor_cannot_add_same_mercari_id_twice(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mX", title="T1", price=1, url="u1")
    db.add_monitor_product(a_id, mercari_id="mX", title="T2", price=2, url="u2")
    rows = db.list_monitor_products(a_id)
    assert len(rows) == 1  # second insert ignored
    assert rows[0][1] == "T1"  # first write wins
    db.close()


def test_different_monitors_can_have_same_mercari_id(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mX", title="A 视角", price=1, url="u")
    db.add_monitor_product(b_id, mercari_id="mX", title="B 视角", price=2, url="u")
    assert db.has_monitor_product(a_id, "mX")
    assert db.has_monitor_product(b_id, "mX")
    assert len(db.list_monitor_products(a_id)) == 1
    assert len(db.list_monitor_products(b_id)) == 1
    db.close()


def test_delete_monitor_product_allows_readd(tmp_path):
    """Delete is NOT ignore: the item may be processed again later."""
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u")
    db.delete_monitor_product(a_id, "mX")
    assert not db.has_monitor_product(a_id, "mX")
    assert not db.is_ignored(a_id, "mX")  # delete must NOT ignore
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u")
    assert db.has_monitor_product(a_id, "mX")
    db.close()


def test_monitor_products_cascade_on_monitor_delete(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u")
    db.add_monitor_product(b_id, mercari_id="mY", title="T", price=1, url="u")
    db.delete_monitor(a_id)
    assert db.list_monitor_products(a_id) == []   # cascaded away
    assert len(db.list_monitor_products(b_id)) == 1  # other monitor intact
    db.close()


# ---------------------------------------------------------- ignored products
def test_ignore_and_is_ignored(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    assert not db.is_ignored(a_id, "mX")
    db.ignore_product(a_id, "mX")
    assert db.is_ignored(a_id, "mX")
    assert len(db.list_ignored_products(a_id)) == 1
    db.close()


def test_ignore_removes_from_history(tmp_path):
    """Ignore must not leave the item in both lists at the same time."""
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u")
    db.ignore_product(a_id, "mX")
    assert db.is_ignored(a_id, "mX")
    assert not db.has_monitor_product(a_id, "mX")  # removed from history
    db.close()


def test_duplicate_ignore_is_idempotent(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.ignore_product(a_id, "mX")
    db.ignore_product(a_id, "mX")
    assert len(db.list_ignored_products(a_id)) == 1
    db.close()


def test_different_monitors_ignore_independently(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.ignore_product(a_id, "mX")
    assert db.is_ignored(a_id, "mX")
    assert not db.is_ignored(b_id, "mX")
    db.close()


def test_unignore_allows_processing_again(tmp_path):
    """Unignore removes the ignored entry only — the item is re-processed
    by a future scan, not auto-added back to the history."""
    db, (a_id, _) = make_db(tmp_path)
    db.ignore_product(a_id, "mX")
    db.unignore_product(a_id, "mX")
    assert not db.is_ignored(a_id, "mX")
    assert not db.has_monitor_product(a_id, "mX")  # NOT re-added
    # and it can be added again by a new scan:
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u")
    assert db.has_monitor_product(a_id, "mX")
    db.close()


def test_ignored_products_cascade_on_monitor_delete(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.ignore_product(a_id, "mX")
    db.ignore_product(b_id, "mY")
    db.delete_monitor(a_id)
    assert not db.is_ignored(a_id, "mX")
    assert db.is_ignored(b_id, "mY")  # other monitor intact
    db.close()


# ------------------------------------------------- Phase 5.3-A: matched flag
def test_unmatched_record_is_processed_but_not_in_history(tmp_path):
    """DeepSeek=false: processed (never re-judged), invisible in history."""
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(
        a_id, mercari_id="mX", title="T", price=1, url="u", matched=False
    )
    assert db.has_monitor_product(a_id, "mX")      # processed
    assert db.list_monitor_products(a_id) == []    # but NOT in the history
    db.close()


def test_history_shows_only_matched(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mA", title="Match", price=1, url="u")
    db.add_monitor_product(a_id, mercari_id="mB", title="Reject", price=2, url="u", matched=False)
    rows = db.list_monitor_products(a_id)
    assert [r[0] for r in rows] == ["mA"]
    db.close()


def test_delete_unmatched_record_allows_reprocessing(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="mX", title="T", price=1, url="u", matched=False)
    db.delete_monitor_product(a_id, "mX")
    assert not db.has_monitor_product(a_id, "mX")  # deletable like any history row
    db.close()


# ------------------------------------------------- Phase 5.4-B: history query
def _seed(db, monitor_id, items):
    for mercari_id, price, published in items:
        db.add_monitor_product(
            monitor_id,
            mercari_id=mercari_id,
            title=f"商品 {mercari_id}",
            price=price,
            url=f"https://jp.mercari.com/item/{mercari_id}",
            published_at=published,
        )


def test_history_published_sorting(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    _seed(db, a_id, [
        ("m1", 1000, "2026-09-10 10:00:00"),
        ("m2", 2000, "2026-09-12 10:00:00"),
        ("m3", 1500, "2026-09-11 10:00:00"),
    ])
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="published_desc")] == ["m2", "m3", "m1"]
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="published_asc")] == ["m1", "m3", "m2"]
    db.close()


def test_history_price_sorting(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    _seed(db, a_id, [
        ("m1", 1000, "2026-09-10 10:00:00"),
        ("m2", 3000, "2026-09-10 10:00:00"),
        ("m3", 2000, "2026-09-10 10:00:00"),
    ])
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="price_desc")] == ["m2", "m3", "m1"]
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="price_asc")] == ["m1", "m3", "m2"]
    db.close()


def test_history_sort_with_null_price(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    _seed(db, a_id, [
        ("m1", 1000, "2026-09-10 10:00:00"),
        ("m2", None, "2026-09-10 10:00:00"),
    ])
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="price_desc")] == ["m1", "m2"]
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="price_asc")] == ["m1", "m2"]
    db.close()


def test_history_sort_with_null_published(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    _seed(db, a_id, [
        ("m1", 1000, "2026-09-10 10:00:00"),
        ("m2", 2000, None),
    ])
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="published_desc")] == ["m1", "m2"]
    assert [r[0] for r in db.list_monitor_products(a_id, sort_by="published_asc")] == ["m1", "m2"]
    db.close()


def test_history_pagination(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    _seed(db, a_id, [
        (f"m{i}", i * 10, f"2026-09-01 00:00:{i % 60:02d}") for i in range(53)
    ])
    page1 = db.list_monitor_products(a_id, limit=20, offset=0)
    page2 = db.list_monitor_products(a_id, limit=20, offset=20)
    page3 = db.list_monitor_products(a_id, limit=20, offset=40)
    assert len(page1) == 20
    assert len(page2) == 20
    assert len(page3) == 13
    db.close()


def test_history_count_only_matched_for_monitor(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="a1", title="T", price=1, url="u")
    db.add_monitor_product(a_id, mercari_id="a2", title="T", price=2, url="u")
    db.add_monitor_product(a_id, mercari_id="a3", title="T", price=3, url="u", matched=False)
    db.add_monitor_product(b_id, mercari_id="b1", title="T", price=4, url="u")
    assert db.count_monitor_products(a_id) == 2  # matched only, monitor A only
    assert db.count_monitor_products(b_id) == 1
    db.close()


def test_history_delete_is_monitor_isolated(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="X", title="A+X", price=1, url="u")
    db.add_monitor_product(b_id, mercari_id="X", title="B+X", price=2, url="u")
    db.delete_monitor_product(a_id, "X")
    assert not db.has_monitor_product(a_id, "X")  # A's record gone
    assert db.has_monitor_product(b_id, "X")      # B's record intact
    assert db.list_monitor_products(b_id)[0][1] == "B+X"
    db.close()


# ------------------------------------------------- Phase 5.4-C: ignored GUI data
def test_ignore_snapshots_title_price_url(tmp_path):
    """Ignoring copies the item snapshot so the ignored list needs no Mercari calls."""
    db, (a_id, _) = make_db(tmp_path)
    db.add_monitor_product(
        a_id, mercari_id="mX", title="商品 X", price=1500,
        url="https://jp.mercari.com/item/mX",
    )
    db.ignore_product(a_id, "mX")
    assert db.is_ignored(a_id, "mX")
    assert not db.has_monitor_product(a_id, "mX")  # removed from history
    mercari_id, title, price, url, ignored_at = db.list_ignored_products(a_id)[0]
    assert mercari_id == "mX"
    assert title == "商品 X"
    assert price == 1500
    assert url == "https://jp.mercari.com/item/mX"
    assert ignored_at
    db.close()


def test_ignore_without_history_record_still_works(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.ignore_product(a_id, "mX")  # not in history — snapshot becomes unknown
    row = db.list_ignored_products(a_id)[0]
    assert row[0] == "mX"
    assert row[1] is None and row[2] is None and row[3] is None
    db.close()


def test_ignored_list_ordering_newest_first(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.ignore_product(a_id, "m1", at="2026-09-15T10:00:00+08:00")
    db.ignore_product(a_id, "m2", at="2026-09-15T12:00:00+08:00")
    db.ignore_product(a_id, "m3", at="2026-09-15T11:00:00+08:00")
    assert [r[0] for r in db.list_ignored_products(a_id)] == ["m2", "m3", "m1"]
    db.close()


def test_ignored_list_pagination(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    for i in range(53):
        db.ignore_product(a_id, f"m{i:03d}", at=f"2026-09-15T10:00:{i % 60:02d}+08:00")
    assert len(db.list_ignored_products(a_id, limit=20, offset=0)) == 20
    assert len(db.list_ignored_products(a_id, limit=20, offset=20)) == 20
    assert len(db.list_ignored_products(a_id, limit=20, offset=40)) == 13
    db.close()


def test_ignored_count_per_monitor(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.ignore_product(a_id, "m1")
    db.ignore_product(a_id, "m2")
    db.ignore_product(b_id, "m3")
    assert db.count_ignored_products(a_id) == 2
    assert db.count_ignored_products(b_id) == 1
    db.close()


def test_ignore_is_monitor_isolated(tmp_path):
    db, (a_id, b_id) = make_db(tmp_path)
    db.add_monitor_product(a_id, mercari_id="X", title="A+X", price=1, url="u")
    db.add_monitor_product(b_id, mercari_id="X", title="B+X", price=2, url="u")
    db.ignore_product(a_id, "X")
    assert db.is_ignored(a_id, "X")
    assert not db.is_ignored(b_id, "X")       # B NOT ignored
    assert db.has_monitor_product(b_id, "X")  # B's history intact
    db.close()


def test_unignore_missing_record_is_noop(tmp_path):
    db, (a_id, _) = make_db(tmp_path)
    db.unignore_product(a_id, "never-ignored")  # must not crash
    assert db.count_ignored_products(a_id) == 0
    db.close()
