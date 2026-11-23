"""Offline regression tests through the application's scan entry point."""

import sqlite3
from unittest.mock import AsyncMock, Mock

import httpx

import pytest

import main
from app.database import Database
from app.deepseek import DeepSeekClient
from app.email import ENV_KEYS
from app.mercari import ItemDetail, SearchItem, SearchPage
from app.outbox import MailOutbox
from app.request_budget import RequestBudget


def test_startup_releases_instance_lock_when_database_migration_fails(monkeypatch):
    lock = Mock()
    monkeypatch.setattr(main, "InstanceLock", lambda: lock)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "Database", Mock(side_effect=sqlite3.OperationalError("migration failed")))
    monkeypatch.delenv("MJM_MERCARI_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.delenv("MJM_DEEPSEEK_REQUESTS_PER_MINUTE", raising=False)

    with pytest.raises(sqlite3.OperationalError, match="migration failed"):
        main.main()
    lock.release.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "   "])
async def test_missing_key_keeps_discovering_without_fetching_details(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY_FOR_MJM", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", key)
    for name in ENV_KEYS:
        monkeypatch.setenv(name, "test@example.test")
    db = Database(":memory:")
    monitor_id = db.create_monitor(name="CD", keywords="CD", ai_requirement="must be a CD")
    item = SearchItem("m1", "CD", 100, "https://example.test", created_epoch=4_000_000_000)
    client = AsyncMock()
    client.search_page.return_value = SearchPage([item], None)
    client.get_item_details.return_value = ItemDetail("CD", "album", None, None)
    ai_http = AsyncMock()
    scan = main.make_scan_handler(
        db, client, DeepSeekClient(ai_http, RequestBudget(30)), MailOutbox(db),
    )
    monkeypatch.setattr("app.scanner.time", lambda: 1000)
    try:
        summary = await scan(monitor_id)
        assert summary is not None
        assert summary == db.get_monitor(monitor_id).last_result
        monkeypatch.setattr("app.scanner.time", lambda: 1301)
        await scan(monitor_id)

        assert client.search_page.await_count == 2
        client.get_item_details.assert_not_awaited()
        ai_http.post.assert_not_awaited()
        assert db.pending_candidates(monitor_id) == 1
        assert db.product_decision(monitor_id, "m1") is None
        assert db.mail_status(monitor_id).pending == 0
        assert "缺少 DeepSeek API Key" in db.get_monitor(monitor_id).last_result

        monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "new-key")
        ai_http.post.return_value = httpx.Response(
            200, json={"choices": [{"message": {"content": "true"}}]},
        )
        await scan(monitor_id)
        await scan(monitor_id)
        client.get_item_details.assert_awaited_once()
        ai_http.post.assert_awaited_once()
        assert db.product_decision(monitor_id, "m1") == (1, 1)
        assert db.pending_candidates(monitor_id) == 0
        assert db.mail_status(monitor_id).pending == 1
    finally:
        db.close()
