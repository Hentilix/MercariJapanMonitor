"""Regression coverage for shared outages, durable details and request budgets."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.database import Database
from app.deepseek import DeepSeekClient
from app.mercari import ItemDetail, MercariClient, SearchItem, SearchPage
from app.request_budget import RequestBudget
from app.scanner import scan_monitor


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_shared_outage_uses_one_probe_and_keeps_details_across_restart(
    tmp_path, monkeypatch, status,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "test-key")
    now = 1000
    probe_started = asyncio.Event()
    finish_probe = asyncio.Event()
    good_response = httpx.Response(200, json={"choices": [{"message": {"content": "true"}}]})

    async def post(*args, **kwargs):
        if http.post.await_count == 1:
            return httpx.Response(status, headers={"Retry-After": "450"})
        if http.post.await_count == 2:
            probe_started.set()
            await finish_probe.wait()
        return good_response

    http = SimpleNamespace(post=AsyncMock(side_effect=post))
    ai = DeepSeekClient(http, RequestBudget(30), clock=lambda: now)
    path = tmp_path / "queue.db"
    db = Database(path)
    first = db.create_monitor(name="first", keywords="CD", ai_requirement="album")
    second = db.create_monitor(name="second", keywords="CD", ai_requirement="album")
    client = AsyncMock()
    client.search_page.return_value = SearchPage([
        SearchItem("m1", "CD", 10, "https://example.test", created_epoch=4_000_000_000),
    ], None)
    client.get_item_details.return_value = ItemDetail("CD", "original description", None, None)

    async def scan(monitor_id):
        monitor = db.get_monitor(monitor_id)
        return await scan_monitor(
            client, db, monitor, judge=ai.make_judge(monitor.ai_requirement), adaptive=True,
        )

    try:
        await scan(first)
        await scan(second)
        assert f"DeepSeek HTTP {status}" in db.get_monitor(second).last_result
        assert db.pending_candidates(first) == db.pending_candidates(second) == 1
        assert client.get_item_details.await_count == http.post.await_count == 1
        assert db._conn.execute(
            "SELECT failure_count FROM scan_candidates ORDER BY monitor_id"
        ).fetchall() == [(0,), (0,)]
        now += 449
        await scan(first)
        assert client.get_item_details.await_count == http.post.await_count == 1

        db.close()
        db = Database(path)
        now += 1
        probing = asyncio.create_task(scan(first))
        try:
            await asyncio.wait_for(probe_started.wait(), 2)
            await scan(second)
            assert "DeepSeek 正在试判" in db.get_monitor(second).last_result
            assert client.get_item_details.await_count == 1
            assert http.post.await_count == 2
        finally:
            finish_probe.set()
            await probing

        assert db.product_decision(first, "m1") == (1, 1)
        assert "original description" in http.post.await_args.kwargs["json"]["messages"][1]["content"]
        await scan(second)
        assert db.product_decision(second, "m1") == (1, 1)
        assert client.get_item_details.await_count == 2
        assert db.candidate_detail(first, "m1") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_mercari_counts_retry_and_shops_fallback_across_concurrent_calls(monkeypatch):
    now = 0
    requests = []

    async def sleep(delay):
        nonlocal now
        now += delay
        await asyncio.sleep(0)

    async def search(*args, **kwargs):
        requests.append(("search", now))
        if sum(name == "search" for name, _ in requests) == 1:
            raise httpx.ReadTimeout("offline")
        return SimpleNamespace(items=[], meta=SimpleNamespace(next_page_token=None))

    async def item(*args):
        requests.append(("item", now))
        return None

    async def product(*args):
        requests.append(("product", now))
        return None

    monkeypatch.setattr("app.mercari.RETRY_DELAYS", (0,))
    client = MercariClient(budget=RequestBudget(1, clock=lambda: now, sleep=sleep))
    client._mercapi = SimpleNamespace(search=search, item=item, product=product)
    try:
        await asyncio.gather(client.search_page("CD"), client.get_item_details("m1"))
        assert requests == [("search", 0), ("item", 60), ("search", 120), ("product", 180)]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rule", "ignore"])
async def test_ai_budget_rechecks_candidate_after_waiting(monkeypatch, change):
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "test-key")
    db = Database(":memory:")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD", ai_requirement="album"))
    now = 0

    async def sleep(delay):
        nonlocal now
        if change == "ignore":
            db.ignore_product(monitor.id, "m1")
        else:
            fields = asdict(monitor)
            for name in ("id", "last_scan_at", "last_result", "created_at", "updated_at", "rule_version"):
                del fields[name]
            db.update_monitor(monitor.id, **(fields | {"ai_requirement": "sealed"}))
        now += delay

    budget = RequestBudget(1, clock=lambda: now, sleep=sleep)
    await budget.acquire()
    http = SimpleNamespace(post=AsyncMock())
    ai = DeepSeekClient(http, budget)
    client = AsyncMock()
    client.search_page.return_value = SearchPage([SearchItem("m1", "CD", 10, "url")], None)
    client.get_item_details.return_value = ItemDetail("CD", "album", None, None)
    try:
        await scan_monitor(
            client, db, monitor, judge=ai.make_judge(monitor.ai_requirement), adaptive=True,
        )
        client.get_item_details.assert_awaited_once()
        http.post.assert_not_awaited()
        assert db.pending_candidates(monitor.id) == (change == "rule")
        assert db.product_decision(monitor.id, "m1") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_rotating_key_reuses_details_and_keeps_shared_ai_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "expired-key")
    now = 0
    requests = []

    async def sleep(delay):
        nonlocal now
        now += delay

    async def post(url, *, json, headers):
        key = headers["Authorization"]
        requests.append((key, now))
        if key == "Bearer expired-key":
            return httpx.Response(401)
        return httpx.Response(200, json={"choices": [{"message": {"content": "false"}}]})

    db = Database(tmp_path / "queue.db")
    monitor = db.get_monitor(db.create_monitor(name="CD", keywords="CD", ai_requirement="album"))
    client = AsyncMock()
    client.search_page.return_value = SearchPage([SearchItem("m1", "CD", 10, "url")], None)
    client.get_item_details.return_value = ItemDetail("CD", "album", None, None)
    budget = RequestBudget(1, clock=lambda: now, sleep=sleep)
    ai = DeepSeekClient(SimpleNamespace(post=post), budget, clock=lambda: now)
    try:
        await scan_monitor(
            client, db, monitor, judge=ai.make_judge(monitor.ai_requirement), adaptive=True,
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "replacement")
        await scan_monitor(
            client, db, monitor, judge=ai.make_judge(monitor.ai_requirement), adaptive=True,
        )
        assert requests == [("Bearer expired-key", 0), ("Bearer replacement", 60)]
        client.get_item_details.assert_awaited_once()
        assert db.product_decision(monitor.id, "m1") == (0, 1)
    finally:
        db.close()


def test_candidate_details_migration_keeps_existing_queue(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    monitor_id = db.create_monitor(name="CD", keywords="CD")
    db._conn.execute("ALTER TABLE scan_candidates DROP COLUMN detail_json")
    db._conn.execute("ALTER TABLE scan_candidates DROP COLUMN failure_count")
    db._conn.executemany(
        """INSERT INTO scan_candidates (monitor_id, mercari_id, title, url, retry_at)
           VALUES (?, ?, ?, ?, ?)""",
        [(monitor_id, "m1", "CD", "url", 0), (monitor_id, "m2", "CD", "url", 1500)],
    )
    db._conn.commit()
    db.close()

    db = Database(path)
    try:
        assert db.pending_candidates(monitor_id) == 2
        assert db.candidate_detail(monitor_id, "m1") is None
        assert db._conn.execute(
            "SELECT retry_at, failure_count FROM scan_candidates ORDER BY id"
        ).fetchall() == [(0, 0), (1500, 1)]
    finally:
        db.close()
