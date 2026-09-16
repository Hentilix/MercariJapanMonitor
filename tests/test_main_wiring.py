"""Wiring-layer tests for main.py's judge construction (P0-2 fix).

The scanner unit tests inject the judge directly, so they could never catch
main.py wiring judge=None when the DeepSeek key is missing — which the old
code silently turned into "match everything". These tests pin the wiring
contract:

  * ai_requirement non-empty -> a judge is ALWAYS built (the API key is
    checked inside judge_item() per call; a missing key becomes per-item AI
    errors, never a silent match-all).
  * ai_requirement empty    -> judge=None (pure Level 1, no-AI gate).

Importing main is offline-safe (ui.run() lives in the guarded block).
"""

import asyncio

import main as main_module

from app.database import Monitor


def make_monitor(ai_requirement: str) -> Monitor:
    return Monitor(
        id=1,
        name="M",
        keywords="kw",
        keyword_mode="AND",
        min_price=None,
        max_price=None,
        filter_words="",
        ai_requirement=ai_requirement,
        interval_minutes=30,
        notification_email=None,
        enabled=True,
        last_scan_at=None,
        last_result=None,
        created_at="",
        updated_at="",
    )


class _FakeDB:
    def __init__(self, monitor: Monitor) -> None:
        self._monitor = monitor

    def get_monitor(self, monitor_id: int) -> Monitor:
        return self._monitor


class _FakeClient:
    pass


def _run_scan_monitor(monkeypatch, ai_requirement: str) -> dict:
    """Run main.scan_monitor(1) with stubbed globals; capture scan kwargs."""
    captured = {}

    async def fake_run_monitor_scan(client, db, mon, *, judge, notifier, max_pages):
        captured["judge"] = judge

    monkeypatch.setattr(main_module, "db", _FakeDB(make_monitor(ai_requirement)), raising=False)
    monkeypatch.setattr(main_module, "client", _FakeClient(), raising=False)
    monkeypatch.setattr(main_module, "ai_http", object(), raising=False)
    monkeypatch.setattr(main_module, "run_monitor_scan", fake_run_monitor_scan)
    asyncio.run(main_module.scan_monitor(1))
    return captured


def test_judge_is_built_when_ai_requirement_present(monkeypatch):
    captured = _run_scan_monitor(monkeypatch, "必须是全新正版日版 CD，带 OBI")
    assert captured["judge"] is not None


def test_judge_is_none_when_no_ai_requirement(monkeypatch):
    captured = _run_scan_monitor(monkeypatch, "")
    assert captured["judge"] is None
