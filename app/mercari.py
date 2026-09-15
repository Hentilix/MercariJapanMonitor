"""Mercari Japan access via mercapi 0.5.0.

Fetch-only. Level 1 filtering lives in app.filter, not here.
"""

from dataclasses import dataclass
from typing import Optional

import httpx
from mercapi import Mercapi
from mercapi.models import Item
from mercapi.models.product import Product

ITEM_URL_BASE = "https://jp.mercari.com/item/"
SHOP_URL_BASE = "https://jp.mercari.com/shops/product/"


class MercariError(Exception):
    """Raised when talking to Mercari fails."""


@dataclass
class SearchItem:
    """The small slice of a Mercari listing that Phase 1 needs."""

    id: str
    title: str
    price: Optional[int]  # None when Mercari marks the item as "no price"
    url: str
    item_type: Optional[str] = None  # e.g. ITEM_TYPE_BEYOND for Mercari Shops
    published_at: Optional[str] = None  # listing publish time, "YYYY-MM-DD HH:MM:SS"


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

    def __init__(self, timeout: float = 15.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)
        self._mercapi = Mercapi(httpx_client=self._http)

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

        `exclude` is passed through to Mercari's single-phrase
        excludeKeyword. List-based exclude keywords are NOT reliable there,
        so they are handled locally by Level 1 (app.filter).
        """
        items: list[SearchItem] = []
        page_token: Optional[str] = None
        for _ in range(max_pages):
            try:
                results = await self._mercapi.search(
                    query,
                    price_min=min_price,
                    price_max=max_price,
                    exclude=exclude,
                    page_token=page_token,
                )
            except Exception as exc:
                raise MercariError(
                    f"Mercari search failed: {type(exc).__name__}: {exc}"
                ) from exc
            items.extend(_to_search_item(item) for item in results.items)
            page_token = results.meta.next_page_token
            if not page_token:
                break
        return items

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
        if item_type == "ITEM_TYPE_BEYOND":
            try:
                full = await self._mercapi.product(item_id)
            except Exception as exc:
                raise MercariError(
                    f"Mercari product fetch failed: {type(exc).__name__}: {exc}"
                ) from exc
            return None if full is None else _to_item_detail(full)

        try:
            full = await self._mercapi.item(item_id)
            if full is None:
                # Not a regular item (or sold) — try the Mercari Shops API.
                full = await self._mercapi.product(item_id)
        except Exception as exc:
            raise MercariError(
                f"Mercari item fetch failed: {type(exc).__name__}: {exc}"
            ) from exc
        if full is None:
            return None
        return _to_item_detail(full)

    async def close(self) -> None:
        await self._http.aclose()
