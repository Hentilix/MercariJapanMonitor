"""Exercise the manual check commands with offline service substitutes."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.database import Database
from app.mercari import ItemDetail, SearchItem, SearchPage
from scripts import test_email as smtp_script
from scripts import test_phase1 as phase1
from scripts import test_phase3 as phase3
from scripts import test_phase4 as phase4


def mock_scan_services(script, monkeypatch, tmp_path):
    path = tmp_path / script.TEST_DB.name
    monkeypatch.setattr(script, "TEST_DB", path)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    client = AsyncMock()
    items = [
        SearchItem(f"m{i}", "Built to Spill CD", 1500, f"https://example.test/{i}")
        for i in range(2)
    ]
    client.search_page.return_value = SearchPage(items, None)
    monkeypatch.setattr(script, "MercariClient", lambda *, budget: client)
    return path, client



def mock_ai_http(script, monkeypatch, response):
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.post.return_value = response
    monkeypatch.setattr(script, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs: http))
    return http


def test_phase3_once_discovers_candidates_in_its_own_database(monkeypatch, tmp_path):
    path, client = mock_scan_services(phase3, monkeypatch, tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY_FOR_MJM", raising=False)
    monkeypatch.setattr(sys, "argv", ["test_phase3.py", "--once"])

    assert phase3.main() == 0
    client.search_page.assert_awaited_once()
    client.get_item_details.assert_not_awaited()
    client.close.assert_awaited_once()

    db = Database(path)
    try:
        monitor = db.get_monitor_by_name(phase3.MONITOR_NAME)
        assert db.pending_candidates(monitor.id) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase4_limit_caps_judgments_in_its_own_database(monkeypatch, tmp_path):
    path, client = mock_scan_services(phase4, monkeypatch, tmp_path)
    client.get_item_details.return_value = ItemDetail("Built to Spill CD", "", None, None)
    ai_http = mock_ai_http(phase4, monkeypatch, httpx.Response(
        200, json={"choices": [{"message": {"content": "true"}}]},
    ))
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "test-key")
    monkeypatch.setattr(sys, "argv", ["test_phase4.py", "--limit", "1"])

    assert await phase4.main() == 0
    ai_http.post.assert_awaited_once()
    client.close.assert_awaited_once()

    db = Database(path)
    try:
        monitor = db.get_monitor_by_name(phase4.MONITOR_NAME)
        assert db.count_monitor_products(monitor.id) == 1
        assert db.pending_candidates(monitor.id) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phase1_searches_one_page_then_the_configured_pages(monkeypatch):
    client = AsyncMock()
    client.search_products.return_value = []
    monkeypatch.setattr(phase1, "MercariClient", lambda: client)
    monkeypatch.setattr(phase1, "asyncio", AsyncMock())

    assert await phase1.main() == 0
    assert [call.kwargs["max_pages"] for call in client.search_products.await_args_list] == [1, 2]
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_smtp_check_sends_one_sample_batch(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(smtp_script, "send_batch", send)

    assert await smtp_script.main() == 0
    send.assert_awaited_once()
    assert len(send.await_args.args[1]) == 3


def test_phase3_once_returns_failure_on_unexpected_scan_error(monkeypatch, tmp_path):
    _, client = mock_scan_services(phase3, monkeypatch, tmp_path)
    client.search_page.side_effect = RuntimeError("unexpected failure")
    monkeypatch.setattr(sys, "argv", ["test_phase3.py", "--once"])

    assert phase3.main() == 1
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_phase4_rate_limit_pauses_remaining_candidates(monkeypatch, tmp_path, caplog):
    path, client = mock_scan_services(phase4, monkeypatch, tmp_path)
    client.get_item_details.return_value = ItemDetail("Built to Spill CD", "", None, None)
    ai_http = mock_ai_http(phase4, monkeypatch, httpx.Response(429))
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "test-key")
    monkeypatch.setattr(sys, "argv", ["test_phase4.py", "--limit", "2"])

    assert await phase4.main() == 0
    ai_http.post.assert_awaited_once()
    client.get_item_details.assert_awaited_once()
    assert "DeepSeek HTTP 429" in caplog.text

    db = Database(path)
    try:
        monitor = db.get_monitor_by_name(phase4.MONITOR_NAME)
        assert db.pending_candidates(monitor.id) == 2
        assert db.count_monitor_products(monitor.id) == 0
    finally:
        db.close()
