"""JPY -> CNY reference exchange rate via the Frankfurter API.

GET https://api.frankfurter.app/latest?from=JPY&to=CNY  (no API key).

Pure UI helper:
  * at most ONE request per calendar day (in-memory cache only)
  * on failure the last successful rate is used as a fallback; when there
    has never been a successful fetch, None is returned (UI shows
    "unavailable") and no exception ever reaches the GUI
  * restarting the process loses the cache, which is fine — the next
    request simply refetches once for that day

Nothing here touches the database, the scanner, the scheduler, DeepSeek
or SMTP. Prices stay JPY everywhere else.
"""

import asyncio
import logging
from datetime import date

import httpx

log = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.app/latest"

_cached_date: date | None = None
_cached_rate: float | None = None
_last_successful_rate: float | None = None
_attempted_today: bool = False
_lock = asyncio.Lock()


def _today() -> date:
    return date.today()


def _reset_cache() -> None:
    """Test helper: reset the in-memory cache to its initial state."""
    global _cached_date, _cached_rate, _last_successful_rate, _attempted_today
    _cached_date = None
    _cached_rate = None
    _last_successful_rate = None
    _attempted_today = False


async def _fetch_jpy_cny(client: httpx.AsyncClient | None) -> float:
    """One raw request. Raises on ANY failure (network/timeout/HTTP/parse/
    validation); the caller turns that into the fallback behaviour."""
    if client is None:
        async with httpx.AsyncClient(timeout=5.0) as new_client:
            return await _fetch_jpy_cny(new_client)

    response = await client.get(
        FRANKFURTER_URL,
        params={"from": "JPY", "to": "CNY"},
        # api.frankfurter.app officially 301-redirects to
        # api.frankfurter.dev/v1 (same provider, new domain) — follow it.
        follow_redirects=True,
    )
    response.raise_for_status()
    body = response.json()
    rate = float(body["rates"]["CNY"])
    if rate <= 0:
        raise ValueError(f"invalid JPY->CNY rate: {rate!r}")
    return rate


async def get_jpy_cny_rate(client: httpx.AsyncClient | None = None) -> float | None:
    """Return the JPY->CNY rate for display, or None when unavailable.

    One request attempt per calendar day. A failed attempt falls back to
    the last successful rate (None if there never was one) and is NOT
    retried until the date changes.
    """
    global _cached_date, _cached_rate, _last_successful_rate, _attempted_today

    async with _lock:
        today = _today()
        if _cached_date == today and _attempted_today:
            return _cached_rate if _cached_rate is not None else _last_successful_rate

        _cached_date = today
        _attempted_today = True
        try:
            rate = await _fetch_jpy_cny(client)
        except Exception as exc:
            log.warning("Frankfurter 汇率获取失败: %s", exc)
            rate = None

        if rate is not None:
            _cached_rate = rate
            _last_successful_rate = rate
            log.info("汇率已更新：1 JPY = %s CNY", rate)
            return rate

        _cached_rate = None
        return _last_successful_rate


def rate_status() -> str:
    """'ok' (fresh today) | 'fallback' (last successful) | 'unavailable'."""
    if _cached_rate is not None:
        return "ok"
    if _last_successful_rate is not None:
        return "fallback"
    return "unavailable"


# ------------------------------------------------------------ UI helpers
def jpy_to_cny(price_jpy: int | None, rate: float | None) -> float | None:
    """Full-precision conversion; rounding happens only at display time."""
    if price_jpy is None or rate is None:
        return None
    return price_jpy * rate


def format_price_pair(price_jpy: int | None, rate: float | None) -> tuple[str, str]:
    """(JPY text, CNY text) for one product row. Never raises."""
    if price_jpy is None:
        return "价格未知", "≈ -- CNY"
    jpy_text = f"\u00a5{price_jpy:,} JPY"
    cny = jpy_to_cny(price_jpy, rate)
    if cny is None:
        return jpy_text, "≈ -- CNY"
    return jpy_text, f"≈ \u00a5{cny:.2f} CNY"


def format_rate(rate: float | None) -> str:
    if rate is None:
        return "CNY 汇率暂不可用"
    return f"参考汇率：1 JPY ≈ {rate:.5f} CNY"
