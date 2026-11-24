import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.database import Database
from app.email import ENV_KEYS
from app.mercari import ItemDetail, SearchItem, SearchPage
from app.scanner import scan_monitor
from app.outbox import MailOutbox


@pytest.fixture
def configured(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.setenv(key, "test")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_TO", "original@example.test")


def item(key):
    return SearchItem(key, f"CD {key}", 123, f"https://example.test/{key}")


@pytest.mark.asyncio
async def test_rediscovered_price_reaches_history_and_mail(tmp_path, configured, monkeypatch):
    path = tmp_path / "test.db"
    db = Database(path)
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD", ai_requirement="CD"))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)
    send = AsyncMock()
    client = AsyncMock()
    original = replace(item("A"), price=100, created_epoch=900)
    client.search_page.return_value = SearchPage([original], None)
    client.get_item_details.return_value = ItemDetail(original.title, "album", None, None)
    judge = AsyncMock(return_value=None)

    async def scan():
        mail = MailOutbox(db, send=send, clock=lambda: now)
        result = await scan_monitor(client, db, mon, outbox=mail, judge=judge)
        await mail.drain_due()
        return result

    try:
        assert (await scan()).ai_errors == 1
        now = 1200
        client.search_page.return_value = SearchPage([replace(original, price=200)], None)
        assert (await scan()).new == 0
        assert db._conn.execute(
            "SELECT price, retry_at, failure_count FROM scan_candidates"
        ).fetchone() == (200, 1300, 1)
        db.close()
        db = Database(path)
        now = 1300
        judge.return_value = True
        assert (await scan()).ai_matched == 1
        assert db.list_monitor_products(mon.id)[0][2] == 200
        await scan()
        send.assert_awaited_once()
        assert send.await_args.args[1][0].price == 200
        client.get_item_details.assert_awaited_once()
        client.get_current_listing.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_hidden_price_increase_blocks_match_and_mail(tmp_path, configured, monkeypatch, cached):
    path = tmp_path / "test.db"
    db = Database(path)
    mon = db.get_monitor(db.create_monitor(
        name="records", keywords="CD", max_price=100, ai_requirement="CD",
    ))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)
    client = AsyncMock()
    original = replace(item("A"), price=90, created_epoch=900)
    detail = ItemDetail(original.title, "album", None, None)
    client.search_page.return_value = SearchPage([original], None)
    client.get_item_details.return_value = detail
    judge = AsyncMock(return_value=None)
    await scan_monitor(client, db, mon, judge=judge if cached else None)
    db.close()

    db = Database(path)
    try:
        now = 1300
        judge.reset_mock()
        judge.return_value = True
        client.search_page.return_value = SearchPage([], None)
        client.get_current_listing.return_value = (200, detail)
        mail = MailOutbox(db, clock=lambda: now)
        await scan_monitor(client, db, mon, judge=judge, outbox=mail)
        judge.assert_not_awaited()
        assert db.list_monitor_products(mon.id) == []
        assert db.mail_status(mon.id).pending == 0
        assert db.pending_candidates(mon.id) == 0

        client.search_page.return_value = SearchPage([replace(original, price=95)], None)
        await scan_monitor(client, db, mon, judge=judge, outbox=mail)
        judge.assert_awaited_once()
        client.get_current_listing.assert_awaited_once()
        assert db.list_monitor_products(mon.id)[0][2] == 95
        assert db.next_due_mail(now)[2][0][2] == 95
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_check", ["offline", "unknown_price", "sold", "ignored", "edited"])
async def test_stale_candidate_verification_controls_direct_match(configured, monkeypatch, first_check):
    from app.mercari import MercariError

    db = Database(":memory:")
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD", max_price=100))
    now = 1000
    monkeypatch.setattr("app.scanner.time", lambda: now)
    client = AsyncMock()
    client.search_page.return_value = SearchPage([], None)
    mail = MailOutbox(db, clock=lambda: now)
    original = item("A")
    detail = ItemDetail(original.title, None, None, None)
    try:
        await scan_monitor(client, db, mon)
        generation, state, _ = db.begin_search(mon, {})
        db.save_search_page(
            mon.id, generation, state,
            [(original.id, original.title, 90, original.url, None, None, None)],
            rule_version=mon.rule_version, queued_at=now,
        )

        async def inspect_listing(*args):
            if first_check == "offline":
                raise MercariError("offline")
            if first_check == "unknown_price":
                return None, detail
            if first_check == "sold":
                return None
            if first_check == "ignored":
                db.ignore_product(mon.id, original.id)
                db.unignore_product(mon.id, original.id)
            if first_check == "edited":
                db.update_monitor(
                    mon.id, name=mon.name, keywords=mon.keywords, keyword_mode=mon.keyword_mode,
                    min_price=None, max_price=80, filter_words="", ai_requirement="",
                    interval_minutes=mon.interval_minutes, notification_email=None, enabled=True,
                )
            return 100, detail

        client.get_current_listing.side_effect = inspect_listing
        await scan_monitor(client, db, mon, outbox=mail)
        assert db.list_monitor_products(mon.id) == []
        assert db.mail_status(mon.id).pending == 0
        if first_check in ("offline", "unknown_price"):
            assert db.pending_candidates(mon.id) == 1
            client.get_current_listing.side_effect = None
            client.get_current_listing.return_value = (100, detail)
            now = 1299
            await scan_monitor(client, db, mon, outbox=mail)
            client.get_current_listing.assert_awaited_once()
            now = 1300
            await scan_monitor(client, db, mon, outbox=mail)
            assert db.list_monitor_products(mon.id)[0][2] == 100
            assert db.next_due_mail(now)[2][0][2] == 100
        else:
            assert db.pending_candidates(mon.id) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_email_survives_restart_without_another_scan(tmp_path, configured, monkeypatch):
    path = tmp_path / "test.db"
    db = Database(path)
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD"))
    client = AsyncMock()
    client.search_page.return_value = SearchPage([item("A"), item("B"), item("A")], None)
    now = 1000
    send = AsyncMock(side_effect=OSError("offline"))
    mail = MailOutbox(db, send=send, clock=lambda: now)
    await scan_monitor(client, db, mon, outbox=mail)
    await scan_monitor(client, db, mon, outbox=mail)
    await mail.drain_due()
    first = send.await_args
    assert [i.id for i in first.args[1]] == ["A", "B"]
    assert first.kwargs["recipient"] == "original@example.test"
    assert first.kwargs["message_id"]
    assert db.mail_status(mon.id).next_attempt_at == 1060
    db.close()

    monkeypatch.setenv("SMTP_TO", "changed@example.test")
    db = Database(path)
    db.set_monitor_enabled(mon.id, False)
    send.reset_mock(side_effect=True)
    mail = MailOutbox(db, send=send, clock=lambda: now)
    await mail.drain_due()
    send.assert_not_awaited()
    now += 60
    await mail.drain_due()
    assert send.await_args == first
    assert db.mail_status(mon.id).pending == 0
    db.close()
    db = Database(path)
    await MailOutbox(db, send=send).drain_due()
    send.assert_awaited_once()
    db.close()


@pytest.mark.asyncio
async def test_outbox_item_failure_rolls_back_match(tmp_path, configured):
    db = Database(tmp_path / "test.db")
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD"))
    client = AsyncMock()
    client.search_page.return_value = SearchPage([item("A")], None)
    db._conn.execute("""
        CREATE TRIGGER reject_mail_item BEFORE INSERT ON mail_items
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
    """)
    outbox = MailOutbox(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            await scan_monitor(client, db, mon, outbox=outbox)
        assert not db.has_monitor_product(mon.id, "A")
        assert db.mail_status(mon.id).pending == 0
        db._conn.execute("DROP TRIGGER reject_mail_item")
        await scan_monitor(client, db, mon, outbox=outbox)
        assert db.has_monitor_product(mon.id, "A")
        assert db.mail_status(mon.id).pending == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_recover_partial_scan_and_retry_ambiguous_delivery(tmp_path, configured):
    path = tmp_path / "test.db"
    db = Database(path)
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD"))
    batch = MailOutbox(db).new_batch(mon)
    db.add_monitor_product(mon.id, mercari_id="A", title="CD A", price=123,
                           url="https://example.test/A", mail_batch=batch)
    assert db.next_due_mail(float("inf")) is None
    db.close()  # process exited before making the batch ready

    db = Database(path)
    send = AsyncMock(side_effect=asyncio.CancelledError)
    outbox = MailOutbox(db, send=send)
    outbox.recover()
    with pytest.raises(asyncio.CancelledError):
        await outbox.drain_due()
    accepted = send.await_args
    db.close()  # SMTP may have accepted DATA before the process exited

    db = Database(path)
    send.reset_mock(side_effect=True)
    await MailOutbox(db, send=send).drain_due()
    assert send.await_args == accepted
    assert db.mail_status(mon.id).pending == 0
    db.close()


@pytest.mark.asyncio
async def test_unconfigured_and_old_matches_are_not_backfilled(tmp_path, configured, monkeypatch):
    path = tmp_path / "test.db"
    db = Database(path)
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD"))
    db.add_monitor_product(mon.id, mercari_id="old", title="CD old", price=None,
                           url="https://example.test/old")
    db._conn.execute("DROP TABLE mail_items")
    db._conn.execute("DROP TABLE mail_batches")
    db.close()

    db = Database(path)
    send = AsyncMock()
    outbox = MailOutbox(db, send=send)
    outbox.recover()
    client = AsyncMock()
    client.search_page.return_value = SearchPage([item("old"), item("A")], None)
    monkeypatch.delenv("SMTP_PASSWORD")
    await scan_monitor(client, db, mon, outbox=outbox)
    monkeypatch.setenv("SMTP_PASSWORD", "restored")
    await scan_monitor(client, db, mon, outbox=outbox)
    await outbox.drain_due()
    send.assert_not_awaited()
    assert db.has_monitor_product(mon.id, "old")
    assert db.has_monitor_product(mon.id, "A")
    db.close()


@pytest.mark.asyncio
async def test_worker_finishes_current_send_and_skips_deleted_batch(tmp_path, configured):
    db = Database(tmp_path / "test.db")
    first = db.get_monitor(db.create_monitor(name="first", keywords="CD"))
    second = db.get_monitor(db.create_monitor(name="second", keywords="CD"))
    client = AsyncMock()
    client.search_page.return_value = SearchPage([item("A")], None)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(label, items, **kwargs):
        entered.set()
        await release.wait()

    smtp = AsyncMock(side_effect=send)
    outbox = MailOutbox(db, send=smtp)
    outbox.start()
    try:
        await scan_monitor(client, db, first, outbox=outbox)
        await asyncio.wait_for(entered.wait(), timeout=2)
        await scan_monitor(client, db, second, outbox=outbox)
        db.delete_monitor(second.id)
        assert db.mail_status(second.id).pending == 0
        stopping = asyncio.create_task(outbox.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        await stopping
        smtp.assert_awaited_once()
        assert db.mail_status(first.id).pending == 0
    finally:
        release.set()
        await outbox.stop()
        db.close()


@pytest.mark.asyncio
async def test_rejected_candidate_does_not_queue_mail(configured):
    db = Database(":memory:")
    mon = db.get_monitor(db.create_monitor(name="records", keywords="CD", ai_requirement="CD"))
    client = AsyncMock()
    client.search_page.return_value = SearchPage([item("A")], None)
    client.get_item_details.return_value = ItemDetail("CD A", "", None, None)
    send = AsyncMock()
    outbox = MailOutbox(db, send=send)
    try:
        result = await scan_monitor(client, db, mon, judge=AsyncMock(return_value=False), outbox=outbox)
        await outbox.drain_due()
        assert result.ai_rejected == 1
        assert db.mail_status(mon.id).pending == 0
        send.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("first_price", "second_price", "first_title", "matched"), [
    (200, 90, "CD album", True),
    (90, 200, "CD album", False),
    (90, 90, "CD album broken", True),
    (90, 90, "CD album", True),
])
async def test_later_or_page_controls_match_and_mail(
    configured, first_price, second_price, first_title, matched,
):
    db = Database(":memory:")
    try:
        mon = db.get_monitor(db.create_monitor(
            name="OR", keywords="CD,album", keyword_mode="OR", max_price=100,
            filter_words="broken",
        ))
        client = AsyncMock()
        first = replace(item("A"), title=first_title, price=first_price)
        second = replace(first, title="CD album", price=second_price)
        client.search_page.side_effect = [SearchPage([first], None), SearchPage([second], None)]
        send = AsyncMock()
        outbox = MailOutbox(db, send=send)
        result = await scan_monitor(client, db, mon, outbox=outbox)
        await outbox.drain_due()
        assert result.found == 1
        assert db.pending_candidates(mon.id) == 0
        history = db.list_monitor_products(mon.id)
        assert len(history) == send.await_count == int(matched)
        if matched:
            assert history[0][1:3] == (second.title, second.price)
            assert [(row.id, row.title, row.price) for row in send.await_args.args[1]] == [
                (second.id, second.title, second.price),
            ]
    finally:
        db.close()
