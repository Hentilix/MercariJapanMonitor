"""Mercari Japan access via mercapi 0.5.0.

Fetch-only. Level 1 filtering lives in app.filter, not here.
"""

from dataclasses import dataclass
import asyncio
import logging
from typing import Optional

import httpx
from mercapi import Mercapi
from mercapi.models import Item
from mercapi.models.product import Product
from mercapi.requests import SearchRequestData

from app.request_budget import RequestBudget, RequestDeferred, check_request

log = logging.getLogger(__name__)

ITEM_URL_BASE = "https://jp.mercari.com/item/"
SHOP_URL_BASE = "https://jp.mercari.com/shops/product/"

# Small retry chain for transient network failures (timeouts, resets).
# TLS/SSL verification errors are deliberately NOT retried: retrying can
# never fix a rejected certificate.
RETRY_DELAYS = (2.0, 4.0)  # seconds before 2nd / 3rd attempt


def _is_transient_network_error(exc: Exception) -> bool:
    """True for failures a short retry can plausibly fix."""
    if isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.ConnectError):
        text = str(exc)
        return not any(
            marker in text for marker in ("SSL", "TLS", "certificate", "CERTIFICATE")
        )
    return False


class MercariError(Exception):
    """Raised when talking to Mercari fails."""


class InvalidPageToken(MercariError):
    """Mercari rejected a pagination token."""


def _rejects_page_token(message: str) -> bool:
    normalized = message.lower().replace("_", "").replace(" ", "").replace("-", "")
    return "pagetoken" in normalized and any(
        word in normalized for word in ("invalid", "expired", "incorrect")
    )


async def _check_search_response(response: httpx.Response) -> None:
    if (response.request.url.path.endswith("/entities:search")
            and response.status_code in (400, 422)):
        await response.aread()
        if _rejects_page_token(response.text):
            raise InvalidPageToken("Mercari rejected the page token")


@dataclass
class SearchItem:
    """The small slice of a Mercari listing that Phase 1 needs."""

    id: str
    title: str
    price: Optional[int]  # None when Mercari marks the item as "no price"
    url: str
    item_type: Optional[str] = None  # e.g. ITEM_TYPE_BEYOND for Mercari Shops
    published_at: Optional[str] = None  # listing publish time, "YYYY-MM-DD HH:MM:SS"
    created_epoch: float | None = None


@dataclass
class SearchPage:
    items: list[SearchItem]
    next_page_token: str | None


@dataclass
class ItemDetail:
    """Fields extracted from a full listing, for the AI judge.

    Only what the semantic filter actually reads. Optional fields are None
    when Mercari does not provide them.
    """

    title: str
    description: Optional[str]
    condition: Optional[str]
    category: Optional[str]


def _to_search_item(raw) -> SearchItem:
    base = SHOP_URL_BASE if raw.item_type == "ITEM_TYPE_BEYOND" else ITEM_URL_BASE
    published = None
    if raw.created:  # mercapi maps this to a datetime from the API timestamp
        published = raw.created.strftime("%Y-%m-%d %H:%M:%S")
    return SearchItem(
        id=raw.id_,
        title=raw.name,
        price=raw.real_price,
        url=f"{base}{raw.id_}",
        item_type=raw.item_type,
        published_at=published,
        created_epoch=raw.created.timestamp() if raw.created else None,
    )


def _to_item_detail(full) -> ItemDetail:
    """Extract title/description/condition/category from full_item() output.

    full_item() returns Item for regular listings and Product for
    Mercari Shops listings (item_type == ITEM_TYPE_BEYOND).
    """
    if isinstance(full, Item):
        category = None
        if full.item_category:
            c = full.item_category
            category = " > ".join(
                filter(None, [c.root_category_name, c.parent_category_name, c.name])
            )
        condition = full.item_condition.name if full.item_condition else None
        return ItemDetail(
            title=full.name or "",
            description=full.description,
            condition=condition,
            category=category,
        )

    # Mercari Shops listing (Product)
    detail = full.product_detail
    description = detail.description if detail else None
    category = None
    if detail and detail.categories:
        category = " > ".join(c.display_name for c in detail.categories)
    condition = detail.condition.display_name if detail and detail.condition else None
    # mercapi's Product.name holds the raw product id; the human-readable
    # title lives in display_name (verified live).
    title = full.display_name or full.name or ""
    return ItemDetail(
        title=title,
        description=description,
        condition=condition,
        category=category,
    )


class MercariClient:
    """Holds one mercapi.Mercapi + one httpx client.

    mercapi's own docs say to avoid instantiating Mercapi more than once
    per runtime, so this class owns a single instance.
    """

    def __init__(self, timeout: float = 15.0, *, budget: RequestBudget | None = None) -> None:
        self._budget = budget
        self._http = httpx.AsyncClient(
            timeout=timeout, event_hooks={"response": [_check_search_response]},
        )
        self._mercapi = Mercapi(httpx_client=self._http)

    async def _request(self, call, *args, **kwargs):
        # Acquire before mercapi creates and timestamps the signed request.
        if self._budget is None:
            check_request()
        else:
            await self._budget.acquire()
        return await call(*args, **kwargs)

    async def search_products(
        self,
        query: str,
        *,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        exclude: Optional[str] = None,
        max_pages: int = 1,
    ) -> list[SearchItem]:
        """Search Mercari for `query`, optionally across several pages.

        mercapi 0.5.0 has no `limit` parameter; each page holds up to ~120
        items and pagination uses `page_token` (verified in Phase 0).

        Only on-sale listings are requested: `status=[STATUS_ON_SALE]`
        excludes sold-out and trading items server-side (verified live —
        the filter returns ITEM_STATUS_ON_SALE results only, for both
        regular items and Mercari Shops listings).

        `exclude` is passed through to Mercari's single-phrase
        excludeKeyword. List-based exclude keywords are NOT reliable there,
        so they are handled locally by Level 1 (app.filter).
        """
        items: list[SearchItem] = []
        token = None
        for _ in range(max_pages):
            page = await self.search_page(
                query, min_price=min_price, max_price=max_price, exclude=exclude,
                page_token=token, newest_first=False,
            )
            items.extend(page.items)
            token = page.next_page_token
            if not token:
                break
        return items

    async def search_page(
        self, query: str, *, page_token: str | None = None,
        min_price: int | None = None, max_price: int | None = None,
        exclude: str | None = None, newest_first: bool = True,
    ) -> SearchPage:
        sort = SearchRequestData.SortBy
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                results = await self._request(
                    self._mercapi.search, query, price_min=min_price, price_max=max_price, exclude=exclude,
                    status=[SearchRequestData.Status.STATUS_ON_SALE],
                    sort_by=sort.SORT_CREATED_TIME if newest_first else sort.SORT_SCORE,
                    sort_order=SearchRequestData.SortOrder.ORDER_DESC,
                    page_token=page_token,
                )
                return SearchPage(
                    [_to_search_item(item) for item in results.items],
                    results.meta.next_page_token or None,
                )
            except (InvalidPageToken, RequestDeferred):
                raise
            except Exception as exc:
                # Mercapi exposes parsing failures rather than raw response
                # statuses. Only reset when the error names the page token.
                if page_token and _rejects_page_token(str(exc)):
                    raise InvalidPageToken(str(exc)) from exc
                if not _is_transient_network_error(exc) or attempt == len(RETRY_DELAYS):
                    raise MercariError(
                        f"Mercari search failed: {type(exc).__name__}: {exc}"
                    ) from exc
                delay = RETRY_DELAYS[attempt]
                log.warning("Mercari search attempt %d failed (%s), retry in %.0fs",
                            attempt + 1, type(exc).__name__, delay)
                await asyncio.sleep(delay)

    async def get_item_details(
        self, item_id: str, item_type: Optional[str] = None
    ) -> Optional[ItemDetail]:
        """Fetch the full listing behind an id and extract the AI-relevant fields.

        Returns None when the listing is gone (HTTP 404). Handles both
        regular items and Mercari Shops listings (ITEM_TYPE_BEYOND).

        Verified live: items/get REJECTS Mercari Shops product ids with
        HTTP 400 {"result":"error",...} (no "data" key) and mercapi 0.5.0
        raises KeyError('data') there. So BEYOND ids are routed straight
        to the shops API — the same dispatch mercapi's own full_item() uses.
        """
        full = await self._get_listing(item_id, item_type)
        return None if full is None else _to_item_detail(full)

    async def get_current_listing(
        self, item_id: str, item_type: str | None = None,
    ) -> tuple[int | None, ItemDetail] | None:
        """Return live price and details, or None for a confirmed unavailable listing."""
        full = await self._get_listing(item_id, item_type, require_confirmed_missing=True)
        if full is None:
            return None
        try:
            if isinstance(full, Item):
                status = (full.status or "").lower().removeprefix("item_status_")
                if status in {"sold_out", "trading", "stop", "closed", "deleted", "cancel"}:
                    return None
                if status != "on_sale":
                    raise ValueError("unknown item sale status")
                raw_price = full.price
            else:
                variants = full.product_detail.variants if full.product_detail else None
                if not variants:
                    raise ValueError("missing Shops inventory")
                quantities = [int(variant.quantity) for variant in variants]
                if any(quantity < 0 for quantity in quantities):
                    raise ValueError("invalid Shops inventory")
                if not any(quantities):
                    return None
                raw_price = full.price
            price = int(raw_price) if raw_price is not None else None
            if price == 9_999_999 and isinstance(full, Item):
                price = None  # Mercari's no-price placeholder
            detail = _to_item_detail(full)
            if (price is not None and price < 0) or not detail.title.strip():
                raise ValueError("invalid listing price or title")
            return price, detail
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise MercariError(f"Cannot verify listing {item_id}: {exc}") from exc

    async def _get_listing(
        self, item_id: str, item_type: str | None, *, require_confirmed_missing: bool = False,
    ) -> Item | Product | None:
        if item_type == "ITEM_TYPE_BEYOND":
            try:
                full = await self._request(self._mercapi.product, item_id)
            except RequestDeferred:
                raise
            except Exception as exc:
                raise MercariError(
                    f"Mercari product fetch failed: {type(exc).__name__}: {exc}"
                ) from exc
            return full

        try:
            full = await self._request(self._mercapi.item, item_id)
            if full is None and (not require_confirmed_missing or item_type != "ITEM_TYPE_MERCARI"):
                # Not a regular item (or sold) — try the Mercari Shops API.
                full = await self._request(self._mercapi.product, item_id)
        except KeyError as exc:
            # items/get answered without a "data" key: either a transient
            # error response (rate limit / interrupted request — the next
            # scan retries) or a Shops listing mislabeled as
            # ITEM_TYPE_MERCARI. Try the Shops API; when it has nothing
            # either, treat the item as temporarily unavailable instead of
            # failing the whole scan with a hard error.
            log.warning(
                "items/get returned no 'data' for %s — trying the Shops API",
                item_id,
            )
            try:
                full = await self._request(self._mercapi.product, item_id)
            except RequestDeferred:
                raise
            except Exception as fallback_error:
                if require_confirmed_missing:
                    raise MercariError(f"Cannot verify listing {item_id}") from fallback_error
                full = None
            if full is None and require_confirmed_missing:
                raise MercariError(f"Cannot verify listing {item_id}") from exc
        except RequestDeferred:
            raise
        except Exception as exc:
            raise MercariError(
                f"Mercari item fetch failed: {type(exc).__name__}: {exc}"
            ) from exc
        return full

    async def close(self) -> None:
        await self._http.aclose()
