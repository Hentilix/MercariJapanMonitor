# -*- coding: utf-8 -*-
"""
test_mercapi.py
Phase 0 — mercapi feasibility test for a Mercari Japan item monitor.

Verified against the ACTUAL installed source of mercapi 0.5.0 (not old tutorials):

  * Mercapi(*, user_agent=None, httpx_client=None)      # keyword-only args
  * await m.search(query, ...)          -> SearchResults
        results.meta.num_found           # total result count (int)
        results.meta.next_page_token     # pagination token
        results.items                    # list of SearchResultItem
  * SearchResultItem fields:
        id_, name, price, seller_id, status, created, updated, thumbnails,
        item_type, item_condition_id, shipping_payer_id, shipping_method_id,
        category_id, is_no_price, auction, real_price (property)
        + async full_item(), async seller()
        NOTE: there is NO built-in URL field; the URL is constructed here.
  * await item.full_item()              -> Item (regular) | Product (Mercari Shops)
  * await m.item(item_id)               -> Item | None (None means HTTP 404)
  * search() has NO "limit" parameter (internal pageSize is fixed at 120);
        pagination = page_token argument or results.next_page() / prev_page()
  * Error surface:
        - network/timeout  -> httpx.RequestError subclasses (ConnectError,
                              TimeoutException, ...)
        - HTTP 403/429/5xx -> NOT raised by mercapi (it never calls
                              raise_for_status); they surface as
                              ParseAPIResponseError / KeyError / JSONDecodeError.
                              This script records the real HTTP status via an
                              httpx event hook to print clear messages.
        - wrong pagination -> IncorrectRequestError (mercapi.util.errors)

Only Python stdlib + httpx + mercapi are used. No NiceGUI / SQLite / etc.
"""

import asyncio
import sys
from typing import Optional, Union

import httpx
from mercapi import Mercapi
from mercapi.models import Item, SearchResultItem, SearchResults
from mercapi.models.product import Product
from mercapi.util.errors import IncorrectRequestError, MercapiError, ParseAPIResponseError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QUERY = "Built to Spill"
ITEMS_TO_PRINT = 10
STABILITY_RUNS = 3
STABILITY_DELAY = 3.0        # seconds between the stability searches
REQUEST_TIMEOUT = 15.0       # seconds, applied to every HTTP request
EMPTY_RESULT_QUERY = "zzqqxx9876543210nothinglikethis"  # for the empty-result check

ITEM_URL_BASE = "https://jp.mercari.com/item/"
SHOP_URL_BASE = "https://jp.mercari.com/shops/product/"

# Official Mercari condition IDs -> labels (useful for your Level-1 filter later)
CONDITION_NAMES = {
    1: "新品、未使用",
    2: "未使用に近い",
    3: "目立った傷や汚れなし",
    4: "やや傷や汚れあり",
    5: "傷や汚れあり",
    6: "全体的に状態が悪い",
}


class HttpStatusRecorder:
    """httpx event hook that remembers the HTTP status of the last response.

    mercapi 0.5.0 never calls raise_for_status() and does not expose the
    status code of failed responses, so we record it here to be able to
    report e.g. "HTTP 403" in error messages.
    """

    def __init__(self) -> None:
        self.last_status: Optional[int] = None
        self.failures = 0

    async def on_response(self, response: httpx.Response) -> None:
        self.last_status = response.status_code


# ---------------------------------------------------------------------------
# Small output helpers
# ---------------------------------------------------------------------------
def banner(text: str) -> None:
    print(f"\n{'=' * 40}\n{text}\n{'=' * 40}")


def format_price(price: Optional[int]) -> str:
    if price is None:
        return "N/A"
    if price == 9999999:  # Mercari's magic value for "no price set"
        return "No price set"
    return f"\u00a5{price:,}"


def item_url(item: SearchResultItem) -> str:
    if item.item_type == "ITEM_TYPE_BEYOND":
        return SHOP_URL_BASE + item.id_
    return ITEM_URL_BASE + item.id_


def describe_error(exc: Exception, recorder: HttpStatusRecorder, action: str) -> None:
    """Print a clear error message instead of a raw traceback."""
    recorder.failures += 1
    if isinstance(exc, httpx.TimeoutException):
        print(f"[ERROR] {action} failed\nReason: timeout (> {REQUEST_TIMEOUT:.0f}s)")
    elif isinstance(exc, httpx.ConnectError):
        print(f"[ERROR] {action} failed\nReason: network connection failed "
              f"(cannot reach api.mercari.jp)")
    elif isinstance(exc, httpx.RequestError):
        print(f"[ERROR] {action} failed\nReason: network error "
              f"({type(exc).__name__}: {exc})")
    elif isinstance(exc, ParseAPIResponseError):
        status = recorder.last_status
        if status:
            hint = {403: " (forbidden — possibly blocked by Mercari anti-bot)",
                    429: " (rate limited — too many requests)"}.get(status, "")
            print(f"[ERROR] {action} failed\nReason: HTTP {status}{hint} "
                  f"(response did not match expected structure)")
        else:
            print(f"[ERROR] {action} failed\nReason: unexpected response structure "
                  f"({type(exc).__name__}: {exc})")
    elif isinstance(exc, KeyError):
        status = recorder.last_status
        print(f"[ERROR] {action} failed\nReason: HTTP {status if status else '?'} — "
              f"response missing expected field {exc}")
    elif isinstance(exc, IncorrectRequestError):
        print(f"[ERROR] {action} failed\nReason: {exc}")
    elif isinstance(exc, MercapiError):
        print(f"[ERROR] {action} failed\nReason: {type(exc).__name__}: {exc}")
    elif isinstance(exc, ValueError):  # e.g. json.JSONDecodeError on non-JSON body
        print(f"[ERROR] {action} failed\nReason: invalid response body "
              f"({type(exc).__name__}: {exc})")
    else:
        print(f"[ERROR] {action} failed\nReason: unexpected {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Wrapped calls (every mercapi call goes through one of these)
# ---------------------------------------------------------------------------
async def safe_search(
    mercapi: Mercapi, recorder: HttpStatusRecorder, query: str
) -> Optional[SearchResults]:
    try:
        return await mercapi.search(query)
    except Exception as exc:  # deliberate catch-all: this is a diagnostic tool
        describe_error(exc, recorder, f"Search '{query}'")
        return None


async def safe_full_item(
    mercapi: Mercapi, recorder: HttpStatusRecorder, item: SearchResultItem
) -> Optional[Union[Item, Product]]:
    try:
        return await item.full_item()
    except Exception as exc:  # deliberate catch-all: this is a diagnostic tool
        describe_error(exc, recorder, f"Item detail '{item.id_}'")
        return None


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def print_search_summary(results: SearchResults) -> None:
    print(f"Found: {results.meta.num_found} items")
    print(f"Items in this page: {len(results.items)}")
    if results.meta.next_page_token:
        print("Next page token: present -> pagination supported")
    else:
        print("Next page token: none -> single page only")


def print_search_items(results: SearchResults, limit: int) -> None:
    for idx, item in enumerate(results.items[:limit], start=1):
        cond = CONDITION_NAMES.get(item.item_condition_id or 0, "N/A")
        print(f"\n[{idx}]")
        print(f"ID: {item.id_}")
        print(f"Title: {item.name}")
        print(f"Price: {format_price(item.real_price)}")
        print(f"URL: {item_url(item)}")
        print(f"Extra: status={item.status}, type={item.item_type}, "
              f"condition={cond}, category_id={item.category_id}, "
              f"seller_id={item.seller_id}")


def _seller_line(seller) -> str:
    if getattr(seller, "ratings", None):
        r = (f"good={seller.ratings.good}/normal={seller.ratings.normal}/"
             f"bad={seller.ratings.bad}")
    else:
        r = "no ratings"
    return (f"{seller.name} (id={seller.id_}, items_sold={seller.num_sell_items}, "
            f"{r}, star={seller.star_rating_score})")


def print_item_detail(full: Union[Item, Product]) -> None:
    if isinstance(full, Item):
        print("Type: regular listing (Item)")
        print(f"ID: {full.id_}")
        print(f"Title: {full.name}")
        print(f"Price: {format_price(full.price)}")
        print(f"Status: {full.status}")
        print(f"Description:\n{full.description if full.description else '(empty)'}")

        if full.item_category:
            cat = full.item_category
            path = " > ".join(filter(None, [cat.root_category_name,
                                            cat.parent_category_name, cat.name]))
            print(f"\nCategory: {path} (id={cat.id_})")
        else:
            print("\nCategory: N/A")

        if full.item_condition:
            print(f"Condition: {full.item_condition.name} (id={full.item_condition.id_})")
        else:
            print("Condition: N/A")

        if full.seller:
            print(f"Seller: {_seller_line(full.seller)}")
        else:
            print("Seller: N/A")

        print(f"\nLikes: {full.num_likes} | Comments: {full.num_comments}")
        print(f"Created: {full.created} | Updated: {full.updated}")
        if full.shipping_payer:
            print(f"Shipping payer: {full.shipping_payer.name}")
        if full.shipping_from_area:
            print(f"Ships from: {full.shipping_from_area.name}")
        if full.shipping_duration:
            print(f"Shipping duration: {full.shipping_duration.name} "
                  f"({full.shipping_duration.min_days}-{full.shipping_duration.max_days} days)")

    elif isinstance(full, Product):
        print("Type: Mercari Shops listing (Product)")
        print("ID: N/A (Product model in mercapi 0.5.0 has no id_ field; "
              "the id is on the search item)")
        print(f"Title: {full.name}")
        print(f"Price: {format_price(full.price)}")
        detail = full.product_detail
        print(f"Description:\n{(detail.description if detail and detail.description else '(empty)')}")
        if detail and detail.categories:
            print(f"\nCategory: {' > '.join(c.display_name for c in detail.categories)}")
        else:
            print("\nCategory: N/A")
        if detail and detail.condition:
            print(f"Condition: {detail.condition.display_name}")
        else:
            print("Condition: N/A")
        if detail and detail.shop:
            print(f"Seller (shop): {detail.shop.display_name}")
        else:
            print("Seller: N/A")

    else:
        print(f"Unknown detail type: {type(full).__name__}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
async def direct_item_test(
    mercapi: Mercapi, recorder: HttpStatusRecorder, item_id: str
) -> None:
    print(f"\n--- Direct fetch by ID ---\nID: {item_id}")
    try:
        direct = await mercapi.item(item_id)
    except Exception as exc:  # deliberate catch-all: this is a diagnostic tool
        describe_error(exc, recorder, f"Item by id '{item_id}'")
        return
    if direct is None:
        print("[WARN] m.item(id) returned None -> HTTP 404 (listing gone/sold)")
    else:
        print(f"OK: {direct.name} / {format_price(direct.price)}")


async def pagination_test(
    mercapi: Mercapi, recorder: HttpStatusRecorder, first: SearchResults
) -> None:
    print(f"\n--- Pagination check (next_page) ---")
    if not first.meta.next_page_token:
        print("Only one page of results available.")
        try:
            await first.next_page()
        except IncorrectRequestError as exc:
            print(f"Verified: next_page() on the last page raises "
                  f"IncorrectRequestError ({exc})")
        return
    try:
        page2 = await first.next_page()
    except Exception as exc:  # deliberate catch-all: this is a diagnostic tool
        describe_error(exc, recorder, "Pagination (next_page)")
        return
    overlap = {i.id_ for i in first.items} & {i.id_ for i in page2.items}
    print(f"Page 1 items: {len(first.items)}")
    print(f"Page 2 items: {len(page2.items)}")
    print(f"num_found consistent: {first.meta.num_found == page2.meta.num_found} "
          f"({page2.meta.num_found})")
    print(f"Overlapping IDs between pages: {len(overlap)}")
    if page2.items:
        print(f"Page 2 first item: {page2.items[0].id_} — {page2.items[0].name}")


async def empty_result_test(
    mercapi: Mercapi, recorder: HttpStatusRecorder
) -> None:
    print(f"\n--- Empty result check ---")
    results = await safe_search(mercapi, recorder, EMPTY_RESULT_QUERY)
    if results is None:
        print("[WARN] Empty-result check could not be completed (see error above).")
        return
    if results.meta.num_found == 0 and len(results.items) == 0:
        print(f"OK: query '{EMPTY_RESULT_QUERY}' returned 0 results and an empty "
              f"item list.")
    else:
        print(f"Unexpected: num_found={results.meta.num_found}, "
              f"items={len(results.items)}")


async def stability_test(
    mercapi: Mercapi, recorder: HttpStatusRecorder
) -> None:
    print(f"\n--- Stability check: {STABILITY_RUNS} identical searches, "
          f"{STABILITY_DELAY:.0f}s apart ---")
    success = 0
    for run in range(1, STABILITY_RUNS + 1):
        results = await safe_search(mercapi, recorder, QUERY)
        if results is not None:
            success += 1
            print(f"Run {run}/{STABILITY_RUNS}: SUCCESS "
                  f"({results.meta.num_found} items, {len(results.items)} on page)")
        else:
            print(f"Run {run}/{STABILITY_RUNS}: FAILED")
        if run < STABILITY_RUNS:
            await asyncio.sleep(STABILITY_DELAY)
    print(f"Stability: {success}/{STABILITY_RUNS} searches succeeded.")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
async def run_phase0(mercapi: Mercapi, recorder: HttpStatusRecorder) -> None:
    # 1. keyword search
    print(f"Searching:\n{QUERY}")
    results = await safe_search(mercapi, recorder, QUERY)
    if results is None:
        print("\n[FATAL] Search failed — aborting the remaining checks.")
        return

    # 2. result count + first items
    print_search_summary(results)
    if not results.items:
        print("[INFO] Empty page — nothing to list or fetch.")
    else:
        print_search_items(results, ITEMS_TO_PRINT)

        # 3. full detail of the first item (via full_item())
        banner("Full Item Detail")
        first = results.items[0]
        full = await safe_full_item(mercapi, recorder, first)
        if full is None:
            print(f"[WARN] Could not fetch full details for {first.id_} "
                  f"(likely HTTP 404 / sold / deleted).")
        else:
            print_item_detail(full)

        # 4. direct fetch by id (the future MercapiClient.get_item() path)
        await direct_item_test(mercapi, recorder, first.id_)

        # 5. pagination
        await pagination_test(mercapi, recorder, results)

    # 6. empty result handling
    await empty_result_test(mercapi, recorder)

    # 7. repeated-request stability
    await stability_test(mercapi, recorder)


async def main() -> int:
    banner("Mercapi Test")
    try:
        import importlib.metadata as md
        version = md.version("mercapi")
    except Exception:
        version = "unknown"
    print(f"mercapi version: {version}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"httpx: {httpx.__version__}")

    recorder = HttpStatusRecorder()
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        event_hooks={"response": [recorder.on_response]},
    )
    try:
        # Mercapi's docs: avoid instantiating more than once per runtime —
        # we create exactly one instance.
        mercapi = Mercapi(httpx_client=client)
        await run_phase0(mercapi, recorder)
    finally:
        await client.aclose()

    if recorder.failures == 0:
        banner("Test completed successfully")
        return 0
    banner(f"Test completed with {recorder.failures} error(s)")
    return 1


if __name__ == "__main__":
    try:
        # Windows console often uses a legacy code page; make sure Japanese
        # titles/descriptions can be printed without UnicodeEncodeError.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
