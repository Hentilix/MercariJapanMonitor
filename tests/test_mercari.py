"""Offline tests for MercariClient detail dispatch (regular vs Mercari Shops).

The fake mercapi backend reproduces the live-verified behavior:
items/get REJECTS Mercari Shops product ids with HTTP 400 (JSON without a
"data" key) and mercapi 0.5.0 raises KeyError('data') for it.
"""

import asyncio

import pytest

from app.mercari import MercariClient, MercariError


class FakeMercapi:
    def __init__(
        self,
        item_result=None,
        item_error=None,
        product_result=None,
        product_error=None,
    ):
        self.item_result = item_result
        self.item_error = item_error
        self.product_result = product_result
        self.product_error = product_error
        self.calls = []

    async def item(self, id_):
        self.calls.append(("item", id_))
        if self.item_error is not None:
            raise self.item_error
        return self.item_result

    async def product(self, product_id):
        self.calls.append(("product", product_id))
        if self.product_error is not None:
            raise self.product_error
        return self.product_result


def make_client(monkeypatch, fake: FakeMercapi) -> MercariClient:
    client = MercariClient()  # offline-safe: no network at construction
    monkeypatch.setattr(client, "_mercapi", fake)
    return client


def run(coro):
    return asyncio.run(coro)


def test_beyond_goes_straight_to_product(monkeypatch):
    fake = FakeMercapi(item_error=KeyError("data"), product_result=None)
    client = make_client(monkeypatch, fake)
    try:
        result = run(
            client.get_item_details("2JWGvFpEzgTBTpivj2GB9A", "ITEM_TYPE_BEYOND")
        )
    finally:
        run(client.close())
    assert result is None  # product returned None -> listing gone
    # item() must never be called for BEYOND ids
    assert fake.calls == [("product", "2JWGvFpEzgTBTpivj2GB9A")]


def test_regular_item_uses_item_then_fallback(monkeypatch):
    fake = FakeMercapi(item_result=None, product_result=None)  # 404 on both
    client = make_client(monkeypatch, fake)
    try:
        result = run(
            client.get_item_details("m62211411605", "ITEM_TYPE_MERCARI")
        )
    finally:
        run(client.close())
    assert result is None
    assert fake.calls == [
        ("item", "m62211411605"),
        ("product", "m62211411605"),
    ]


def test_unknown_type_uses_item_with_keyerror_fallback(monkeypatch):
    """KeyError from items/get no longer crashes the scan: the Shops API is
    tried and, when it has nothing, the item counts as unavailable (retried
    next scan) — the same fallback regular items already had for None."""
    fake = FakeMercapi(item_error=KeyError("data"), product_result=None)
    client = make_client(monkeypatch, fake)
    try:
        result = run(client.get_item_details("2JWGvFpEzgTBTpivj2GB9A", None))
    finally:
        run(client.close())
    assert result is None
    assert fake.calls == [
        ("item", "2JWGvFpEzgTBTpivj2GB9A"),
        ("product", "2JWGvFpEzgTBTpivj2GB9A"),
    ]


def test_beyond_product_error_is_wrapped(monkeypatch):
    fake = FakeMercapi(product_error=ValueError("boom"))
    client = make_client(monkeypatch, fake)
    try:
        with pytest.raises(MercariError) as excinfo:
            run(
                client.get_item_details("2JWGvFpEzgTBTpivj2GB9A", "ITEM_TYPE_BEYOND")
            )
    finally:
        run(client.close())
    assert "Mercari product fetch failed" in str(excinfo.value)


# -------------------------------------------------- Product field extraction
def make_product_detail(**overrides) -> "ProductDetail":
    from mercapi.models.product import ProductDetail

    fields = dict(
        shop=None, photos=None, description=None, categories=None,
        brand=None, condition=None, shipping_method=None, shipping_payer=None,
        shipping_duration=None, shipping_from_area=None, promotions=None,
        product_stats=None, time_sale_details=None, variants=None,
        shipping_fee_config=None, variation_grouping=None,
    )
    fields.update(overrides)
    return ProductDetail(**fields)


def make_product(**overrides) -> "Product":
    from mercapi.models.product import Product

    fields = dict(
        name="2JWGvFpEzgTBTpivj2GB9A",
        price=1194,
        display_name=None,
        product_tags=None,
        thumbnail=None,
        create_time=None,
        update_time=None,
        attributes=None,
        product_detail=make_product_detail(),
    )
    fields.update(overrides)
    return Product(**fields)


def test_product_detail_uses_display_name_for_title():
    from mercapi.models.product import ProductDetail

    from app.mercari import _to_item_detail

    product = make_product(
        display_name="【中古】 The Normal Years [import] / Built To Spill",
        product_detail=make_product_detail(
            description="CD の説明",
            condition=ProductDetail.Condition(display_name="やや傷や汚れあり"),
            categories=[
                ProductDetail.Category(
                    category_id=1,
                    display_name="CD",
                    parent_id=0,
                    root_id=0,
                    has_child=False,
                )
            ],
        ),
    )
    detail = _to_item_detail(product)
    assert detail.title == "【中古】 The Normal Years [import] / Built To Spill"
    assert detail.condition == "やや傷や汚れあり"
    assert detail.category == "CD"
    assert detail.description == "CD の説明"


def test_product_detail_falls_back_to_id_when_no_display_name():
    from app.mercari import _to_item_detail

    product = make_product(display_name=None)
    detail = _to_item_detail(product)
    assert detail.title == "2JWGvFpEzgTBTpivj2GB9A"
    assert detail.condition is None
    assert detail.category is None


# -------------------------------------- items/get without "data" (KeyError)
def test_item_keyerror_with_no_shops_listing_returns_none(monkeypatch):
    """A transient items/get response without 'data' must not crash the scan:
    try the Shops API, then treat as unavailable (retried next scan)."""
    fake = FakeMercapi(item_error=KeyError("data"), product_result=None)
    client = make_client(monkeypatch, fake)
    try:
        result = run(client.get_item_details("m96352361528"))
    finally:
        run(client.close())
    assert result is None
    assert fake.calls == [("item", "m96352361528"), ("product", "m96352361528")]


def test_item_keyerror_uses_shops_listing_when_available(monkeypatch):
    """The same KeyError with an existing Shops product returns its detail."""
    fake = FakeMercapi(
        item_error=KeyError("data"),
        product_result=make_product(display_name="Shops CD"),
    )
    client = make_client(monkeypatch, fake)
    try:
        detail = run(client.get_item_details("m96352361528"))
    finally:
        run(client.close())
    assert detail is not None
    assert detail.title == "Shops CD"
    assert fake.calls == [("item", "m96352361528"), ("product", "m96352361528")]
