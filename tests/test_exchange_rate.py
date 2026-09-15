"""Offline tests for the JPY->CNY exchange rate helper (Frankfurter, mocked).

The real Frankfurter API is never called here.
"""

import asyncio
from datetime import date

import httpx
import pytest

import app.exchange_rate as exchange_rate
from app.exchange_rate import (
    format_price_pair,
    format_rate,
    get_jpy_cny_rate,
    jpy_to_cny,
)


def run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


class FakeHttp:
    def __init__(self, responses=None, error=None):
        self._responses = list(responses or [])
        self._error = error
        self.calls = 0
        self.last_kwargs = None

    async def get(self, url, params=None, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    exchange_rate._reset_cache()
    monkeypatch.setattr(exchange_rate, "_today", lambda: date(2026, 9, 15))
    yield
    exchange_rate._reset_cache()


def ok_response(rate=0.046):
    return FakeResponse(200, {"amount": 1.0, "base": "JPY",
                              "date": "2026-09-15", "rates": {"CNY": rate}})


# ---------------------------------------------------------------- fetch cases
def test_success_returns_rate():
    http = FakeHttp([ok_response(0.046)])
    assert run(get_jpy_cny_rate(client=http)) == 0.046
    # the request follows Frankfurter's official 301 redirect
    assert http.last_kwargs.get("follow_redirects") is True


def test_http_error_returns_none_without_history():
    http = FakeHttp([FakeResponse(500, {"error": "boom"})])
    assert run(get_jpy_cny_rate(client=http)) is None


def test_timeout_returns_none_without_history():
    http = FakeHttp(error=httpx.ConnectTimeout("timeout"))
    assert run(get_jpy_cny_rate(client=http)) is None


def test_invalid_json_returns_none_without_history():
    http = FakeHttp([FakeResponse(200, payload=None)])  # json() raises
    assert run(get_jpy_cny_rate(client=http)) is None


def test_missing_rates_returns_none_without_history():
    http = FakeHttp([FakeResponse(200, {"amount": 1.0})])
    assert run(get_jpy_cny_rate(client=http)) is None


def test_missing_cny_returns_none_without_history():
    http = FakeHttp([FakeResponse(200, {"rates": {}})])
    assert run(get_jpy_cny_rate(client=http)) is None


def test_non_numeric_cny_returns_none_without_history():
    http = FakeHttp([FakeResponse(200, {"rates": {"CNY": "abc"}})])
    assert run(get_jpy_cny_rate(client=http)) is None


def test_non_positive_cny_returns_none_without_history():
    http = FakeHttp([FakeResponse(200, {"rates": {"CNY": 0}})])
    assert run(get_jpy_cny_rate(client=http)) is None


def test_never_succeeded_failure_raises_nothing():
    http = FakeHttp(error=httpx.ConnectError("down"))
    assert run(get_jpy_cny_rate(client=http)) is None  # no exception to GUI


# ------------------------------------------------------------ daily caching
def test_one_request_per_day():
    http = FakeHttp([ok_response(0.046)])
    assert run(get_jpy_cny_rate(client=http)) == 0.046
    assert run(get_jpy_cny_rate(client=http)) == 0.046
    assert run(get_jpy_cny_rate(client=http)) == 0.046
    assert http.calls == 1


def test_new_day_refetches(monkeypatch):
    http = FakeHttp([ok_response(0.046), ok_response(0.047)])
    assert run(get_jpy_cny_rate(client=http)) == 0.046
    assert run(get_jpy_cny_rate(client=http)) == 0.046  # same day -> cached
    monkeypatch.setattr(exchange_rate, "_today", lambda: date(2026, 9, 16))
    assert run(get_jpy_cny_rate(client=http)) == 0.047  # new day -> refetch
    assert http.calls == 2


# ------------------------------------------------------------------ fallback
def test_failure_uses_last_successful_and_stops_retrying(monkeypatch):
    http = FakeHttp([ok_response(0.046)])
    assert run(get_jpy_cny_rate(client=http)) == 0.046

    monkeypatch.setattr(exchange_rate, "_today", lambda: date(2026, 9, 16))  # new day
    failing = FakeHttp(error=httpx.ConnectError("down"))
    assert run(get_jpy_cny_rate(client=failing)) == 0.046  # fallback to last good
    assert failing.calls == 1
    # same day: no further requests even when asked again
    assert run(get_jpy_cny_rate(client=failing)) == 0.046
    assert failing.calls == 1


def test_rate_status_transitions(monkeypatch):
    assert exchange_rate.rate_status() == "unavailable"
    http = FakeHttp([ok_response(0.046)])
    run(get_jpy_cny_rate(client=http))
    assert exchange_rate.rate_status() == "ok"
    monkeypatch.setattr(exchange_rate, "_today", lambda: date(2026, 9, 16))
    run(get_jpy_cny_rate(client=FakeHttp(error=httpx.ConnectError("down"))))
    assert exchange_rate.rate_status() == "fallback"


# ---------------------------------------------------------------- conversion
def test_conversion_uses_full_precision():
    assert jpy_to_cny(3000, 0.04612345) == pytest.approx(138.37035)


def test_conversion_none_price_or_rate():
    assert jpy_to_cny(None, 0.046) is None
    assert jpy_to_cny(3000, None) is None


def test_format_price_pair():
    assert format_price_pair(3000, 0.04612345) == (
        "\u00a53,000 JPY", "≈ \u00a5138.37 CNY",
    )


def test_format_price_pair_unknown():
    assert format_price_pair(None, 0.046) == ("价格未知", "≈ -- CNY")
    assert format_price_pair(3000, None) == ("\u00a53,000 JPY", "≈ -- CNY")


def test_format_rate():
    assert format_rate(0.04612345) == "参考汇率：1 JPY ≈ 0.04612 CNY"
    assert format_rate(None) == "CNY 汇率暂不可用"
