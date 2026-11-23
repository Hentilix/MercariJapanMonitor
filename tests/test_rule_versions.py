import asyncio
import sqlite3
from dataclasses import asdict, replace

import pytest

from app.database import Database
from app.email import ENV_KEYS
from app.mercari import ItemDetail, SearchItem, SearchPage
from app.outbox import MailOutbox
from app.scanner import scan_monitor


class Listings:
    item = SearchItem("m1", "CD", 100, "https://example.test/m1", created_epoch=1)
    items = [item]

    async def search_page(self, *args, **kwargs):
        return SearchPage(self.items, None)

    async def get_item_details(self, *args):
        return ItemDetail("CD", "album", None, None)


def edit(db, mid, **changes):
    fields = asdict(db.get_monitor(mid))
    for key in ("id", "last_scan_at", "last_result", "created_at", "updated_at", "rule_version"):
        fields.pop(key, None)
    db.update_monitor(mid, **(fields | changes))


def test_old_rejection_can_become_first_match(tmp_path):
    db = Database(tmp_path / "scan.db")
    mid = db.create_monitor(name="CD", keywords="CD", ai_requirement="old")
    listings = Listings()
    calls = []
    matched = False

    async def judge(detail):
        calls.append(detail.title)
        return matched

    def scan():
        return asyncio.run(scan_monitor(
            listings, db, db.get_monitor(mid), judge=judge,
        ))

    assert scan().ai_rejected == 1
    edit(db, mid, notification_email="new@example.test")
    assert scan().ai_rejected == 0
    edit(db, mid, ai_requirement="new")
    listings.items = []
    assert scan().ai_matched == 0
    listings.items = [listings.item]
    matched = True
    assert scan().ai_matched == 1
    edit(db, mid, ai_requirement="another")
    assert scan().ai_matched == 0
    assert calls == ["CD", "CD"]
    assert db.count_monitor_products(mid, matched=0) == 0
    db.close()


def test_edit_then_revert_during_judgment_discards_old_result(tmp_path):
    db = Database(tmp_path / "scan.db")
    mid = db.create_monitor(name="CD", keywords="CD", ai_requirement="old")

    async def judge(detail):
        edit(db, mid, ai_requirement="new")
        edit(db, mid, ai_requirement="old")
        return True

    result = asyncio.run(scan_monitor(
        Listings(), db, db.get_monitor(mid), judge=judge,
    ))
    assert result.ai_matched == 0
    assert db.count_monitor_products(mid, matched=None) == 0
    assert db.get_monitor(mid).last_scan_at is None
    assert db.pending_candidates(mid) == 1
    db.close()


@pytest.mark.asyncio
async def test_promotion_and_mail_commit_together(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.setenv(key, "test")
    db = Database(tmp_path / "scan.db")
    outbox = MailOutbox(db)
    mid = db.create_monitor(name="CD", keywords="CD", ai_requirement="old")
    listings = Listings()
    matched = False

    async def judge(detail):
        return matched

    async def scan():
        return await scan_monitor(
            listings, db, db.get_monitor(mid), judge=judge, outbox=outbox,
        )

    assert (await scan()).ai_rejected == 1
    edit(db, mid, ai_requirement="new")
    listings.items = [replace(listings.item, title="CD edited", price=120)]
    matched = True
    db._conn.execute("""
        CREATE TRIGGER fail_mail BEFORE INSERT ON mail_items
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        await scan()
    assert db.product_decision(mid, "m1") == (0, 1)
    assert db.pending_candidates(mid) == 1
    assert db.mail_status(mid).pending == 0

    db._conn.execute("DROP TRIGGER fail_mail")
    assert (await scan()).ai_matched == 1
    assert db.product_decision(mid, "m1") == (1, 2)
    assert db.pending_candidates(mid) == 0
    edit(db, mid, ai_requirement="newer")
    assert (await scan()).ai_matched == 0
    assert db.mail_status(mid).pending == 1
    _, _, items = db.next_due_mail(float("inf"))
    assert items == [("m1", "CD edited", 120, listings.item.url)]
    db.close()


@pytest.mark.asyncio
async def test_legacy_rejections_retry_after_migration_and_restart(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    mid = db.create_monitor(name="CD", keywords="CD", ai_requirement="req")
    for key, matched in (("m1", False), ("matched", True), ("ignored", False)):
        db.add_monitor_product(mid, mercari_id=key, title="CD", price=100, url="u", matched=matched)
    db.ignore_product(mid, "ignored")
    db._conn.execute("ALTER TABLE monitors DROP COLUMN rule_version")
    db._conn.execute("ALTER TABLE monitor_products DROP COLUMN judged_rule_version")
    db.close()
    listings = Listings()
    listings.items = [
        listings.item, replace(listings.item, id="matched"), replace(listings.item, id="ignored"),
    ]
    db = Database(path)
    assert db.product_decision(mid, "m1") == (0, None)
    assert db.product_decision(mid, "matched") == (1, None)
    db.close()

    db = Database(path)
    calls = []

    async def reject(detail):
        calls.append(detail)
        return False

    monitor = db.get_monitor(mid)
    result = await scan_monitor(listings, db, monitor, judge=reject)
    assert (result.ai_rejected, result.ignored) == (1, 1)
    assert db.product_decision(mid, "m1") == (0, 1)
    assert db.list_monitor_products(mid, include_decision=True)[0][-2:] == (1, None)
    db.close()

    db = Database(path)
    await scan_monitor(listings, db, db.get_monitor(mid), judge=reject)
    assert len(calls) == 1
    db.close()


@pytest.mark.asyncio
async def test_deep_recheck_with_unknown_date_keeps_rejection_until_retry(tmp_path, monkeypatch):
    class DeepListings(Listings):
        async def search_page(self, query, *, page_token, **kwargs):
            page = int(page_token or 1)
            return SearchPage(
                [replace(self.item, created_epoch=None)] if page == 4 else [],
                str(page + 1) if page < 4 else None,
            )

    db = Database(tmp_path / "scan.db")
    mid = db.create_monitor(name="CD", keywords="CD", ai_requirement="old")
    now = 1000
    verdict = False
    monkeypatch.setattr("app.scanner.time", lambda: now)

    async def judge(detail):
        return verdict

    await scan_monitor(Listings(), db, db.get_monitor(mid), judge=judge)
    edit(db, mid, ai_requirement="new")
    monitor = db.get_monitor(mid)
    verdict = None
    result = await scan_monitor(DeepListings(), db, monitor, judge=judge)
    assert result.ai_errors == 1
    assert db.product_decision(mid, "m1") == (0, 1)
    assert db.pending_candidates(mid) == 1

    verdict = False
    now += 300
    result = await scan_monitor(DeepListings(), db, monitor, judge=judge)
    assert result.ai_rejected == 1
    assert db.product_decision(mid, "m1") == (0, 2)
    assert db.pending_candidates(mid) == 0
    db.close()


def test_version_and_search_progress_change_only_for_relevant_edits(tmp_path):
    db = Database(tmp_path / "scan.db")
    mid = db.create_monitor(name="A", keywords="CD,album", filter_words=" DVD ", ai_requirement="req")
    other = db.create_monitor(name="B", keywords="CD")
    generation, _, _ = db.begin_search(db.get_monitor(mid), {})

    edit(db, mid, name="A renamed", keywords=" CD ， album ", filter_words="DVD",
         ai_requirement=" req ", interval_minutes=15, enabled=False)
    assert db.get_monitor(mid).rule_version == 1
    assert db.search_is_current(mid, generation)
    edit(db, mid, ai_requirement="new")
    assert db.get_monitor(mid).rule_version == 2
    assert db.search_is_current(mid, generation)
    edit(db, mid, max_price=999, ai_requirement="latest")
    assert db.get_monitor(mid).rule_version == 3
    assert not db.search_is_current(mid, generation)
    assert db.get_monitor(other).rule_version == 1
    db.close()
