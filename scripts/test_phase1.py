# -*- coding: utf-8 -*-
"""Phase 1 integration test — hits the real Mercari Japan API.

Run from the project root:
    python scripts/test_phase1.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.filter import FilterRule, apply_filter  # noqa: E402
from app.mercari import MercariClient, MercariError  # noqa: E402

QUERY = "Built to Spill"
MAX_PAGES = 2

RULE = FilterRule(
    keywords=["Built to Spill"],
    min_price=1000,
    max_price=5000,
    exclude_keywords=["LP", "DVD", "Blu-ray"],
)


def fmt_price(price: int | None) -> str:
    return "no price" if price is None else f"\u00a5{price:,}"


def print_items(items, limit: int) -> None:
    for idx, item in enumerate(items[:limit], start=1):
        print(f"[{idx}] {item.id} | {item.title} | {fmt_price(item.price)} | {item.url}")


async def main() -> int:
    print("=" * 40)
    print("Phase 1 Integration Test")
    print("=" * 40)
    print(f"Query: {QUERY}")
    print(f"Rule: keywords={RULE.keywords}, "
          f"min_price={RULE.min_price}, max_price={RULE.max_price}, "
          f"exclude={RULE.exclude_keywords}")

    client = MercariClient()
    try:
        try:
            page1 = await client.search_products(QUERY, max_pages=1)
            await asyncio.sleep(2.0)  # be gentle with the API
            items = await client.search_products(QUERY, max_pages=MAX_PAGES)
        except MercariError as exc:
            print(f"[ERROR] Mercari search failed: {exc}")
            return 1

        print(f"\nPage 1: {len(page1)} items")
        print(f"Pages 1-{MAX_PAGES}: {len(items)} items")
        if len(items) > len(page1):
            print("Pagination OK: page 2 was fetched.")
        elif MAX_PAGES > 1:
            print("Note: no further results beyond page 1 "
                  "(next_page_token was empty).")

        print(f"\nFirst 5 items (before filtering):")
        print_items(items, 5)

        print("\nApplying Level 1 filter...")
        candidates = apply_filter(items, RULE)
        print(f"Before: {len(items)} | After: {len(candidates)}")

        print(f"\nFirst 10 candidates:")
        if candidates:
            print_items(candidates, 10)
        else:
            print("(none)")
    finally:
        await client.close()

    print("\n" + "=" * 40)
    print("Phase 1 integration test finished")
    print("=" * 40)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
