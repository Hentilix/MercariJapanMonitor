"""Offline unit tests for Level 1 filtering. No network involved."""

import pytest

from app.filter import (
    FilterRule,
    apply_filter,
    matches_exclude,
    matches_keyword,
    matches_price,
    passes,
)
from app.mercari import SearchItem


def make_item(title="Title", price=1000, item_id="m0001") -> SearchItem:
    return SearchItem(
        id=item_id,
        title=title,
        price=price,
        url=f"https://jp.mercari.com/item/{item_id}",
    )


# ---------------------------------------------------------------- keyword
def test_keyword_match():
    assert matches_keyword("Built to Spill - Perfect From Now On", ["Built to Spill"])


def test_keyword_case_insensitive():
    assert matches_keyword("Built To Spill - CD", ["built to spill"])


def test_keyword_no_match():
    assert not matches_keyword("Radiohead - OK Computer", ["Built to Spill"])


def test_keyword_multiple_all_required():
    keywords = ["Built to Spill", "CD"]
    assert matches_keyword("Built to Spill Keep It Like a Secret CD", keywords)
    assert not matches_keyword("Built to Spill Keep It Like a Secret", keywords)


# ------------------------------------------------------------------- modes
def test_keyword_or_mode_any_match():
    keywords = ["Radiohead", "CD"]
    assert matches_keyword("Built to Spill - CD", keywords, mode="OR")
    assert matches_keyword("Radiohead - OK Computer", keywords, mode="OR")
    assert not matches_keyword("Built to Spill - LP", keywords, mode="OR")


def test_keyword_or_mode_empty_keywords():
    assert matches_keyword("anything", [], mode="OR")


def test_keyword_default_mode_is_and():
    keywords = ["Built to Spill", "CD"]
    assert matches_keyword("Built to Spill - CD", keywords)
    assert not matches_keyword("Radiohead - CD", keywords)


def test_rule_or_mode():
    rule = FilterRule(keywords=["Radiohead", "Built to Spill"], match_mode="OR")
    assert passes(make_item("Built to Spill - CD"), rule)
    assert not passes(make_item("Nirvana - LP"), rule)


def test_rule_and_mode_default():
    rule = FilterRule(keywords=["Built to Spill", "CD"])
    assert rule.match_mode == "AND"
    assert passes(make_item("Built to Spill - CD"), rule)
    assert not passes(make_item("Built to Spill - LP"), rule)


def test_rule_mode_case_insensitive():
    rule = FilterRule(keywords="Built to Spill", match_mode="or")
    assert rule.match_mode == "OR"


def test_rule_invalid_mode_rejected():
    with pytest.raises(ValueError):
        FilterRule(keywords="x", match_mode="XOR")


def test_rule_or_mode_with_exclude_and_price():
    rule = FilterRule(
        keywords=["Built to Spill", "Radiohead"],
        exclude_keywords=["LP"],
        min_price=1000,
        max_price=5000,
        match_mode="OR",
    )
    assert passes(make_item("Built to Spill - CD", 1200), rule)      # OR + ok
    assert not passes(make_item("Built to Spill - LP", 1200), rule)  # excluded
    assert not passes(make_item("Nirvana - CD", 1200), rule)         # OR fails
    assert not passes(make_item("Radiohead - CD", 600), rule)        # price fails


def test_keyword_empty_list_matches_anything():
    assert matches_keyword("anything at all", [])


def test_keyword_none_title():
    assert not matches_keyword(None, ["Built to Spill"])
    assert matches_keyword(None, [])


def test_keyword_japanese_title():
    assert matches_keyword("ビルト・トゥ・スピル - CD", ["ビルト"])
    assert not matches_keyword("ビルト・トゥ・スピル - CD", ["ラジオヘッド"])


# ----------------------------------------------------------------- price
def test_price_below_min_fails():
    assert not matches_price(999, 1000, None)


def test_price_equal_min_passes():
    assert matches_price(1000, 1000, None)


def test_price_above_max_fails():
    assert not matches_price(5001, None, 5000)


def test_price_equal_max_passes():
    assert matches_price(5000, None, 5000)


def test_price_no_bounds_passes():
    assert matches_price(123456, None, None)


def test_price_zero_with_no_bounds_passes():
    assert matches_price(0, None, None)


def test_price_zero_with_min_fails():
    assert not matches_price(0, 1000, None)


def test_price_zero_with_min_zero_passes():
    assert matches_price(0, 0, None)


def test_price_none_with_bounds_fails():
    assert not matches_price(None, 1000, None)
    assert not matches_price(None, None, 5000)


def test_price_none_without_bounds_passes():
    assert matches_price(None, None, None)


# ---------------------------------------------------------------- exclude
def test_exclude_hit_fails():
    assert not matches_exclude("Radiohead - OK Computer LP", ["LP"])


def test_exclude_no_hit_passes():
    assert matches_exclude("Radiohead - OK Computer CD", ["LP"])


def test_exclude_case_insensitive():
    assert not matches_exclude("Radiohead - OK Computer lp", ["LP"])


def test_exclude_empty_list_passes():
    assert matches_exclude("Radiohead - OK Computer LP", [])


def test_exclude_none_title_passes():
    assert matches_exclude(None, ["LP"])


def test_exclude_beats_keyword():
    rule = FilterRule(keywords="Radiohead", exclude_keywords=["LP"])
    assert not passes(make_item("Radiohead - OK Computer LP"), rule)


# -------------------------------------------------------------- combined
def test_combined_rule():
    rule = FilterRule(
        keywords="Built to Spill",
        exclude_keywords=["LP", "DVD"],
        min_price=1000,
        max_price=5000,
    )
    assert passes(make_item("Built to Spill - Keep It Like a Secret CD", 3000), rule)
    # exclude wins over keyword
    assert not passes(make_item("Built to Spill - Keep It Like a Secret LP", 3000), rule)
    # price below min
    assert not passes(make_item("Built to Spill - Keep It Like a Secret CD", 999), rule)
    # price above max
    assert not passes(make_item("Built to Spill - Keep It Like a Secret CD", 5001), rule)
    # keyword missing
    assert not passes(make_item("Radiohead - OK Computer CD", 3000), rule)


def test_combined_boundaries_inclusive():
    rule = FilterRule(keywords="Built to Spill", min_price=1000, max_price=5000)
    assert passes(make_item("Built to Spill - A", 1000), rule)
    assert passes(make_item("Built to Spill - B", 5000), rule)


def test_apply_filter_returns_only_matches():
    rule = FilterRule(
        keywords="Built to Spill",
        exclude_keywords=["LP"],
        min_price=1000,
        max_price=5000,
    )
    items = [
        make_item("Built to Spill A CD", 1200),
        make_item("Built to Spill B LP", 1200),
        make_item("Built to Spill C CD", 600),
        make_item("Radiohead D CD", 1200),
    ]
    result = apply_filter(items, rule)
    assert len(result) == 1
    assert result[0].title == "Built to Spill A CD"


# ---------------------------------------------------------------- rule
def test_rule_accepts_plain_strings():
    rule = FilterRule(keywords="Built to Spill", exclude_keywords="LP")
    assert rule.keywords == ["Built to Spill"]
    assert rule.exclude_keywords == ["LP"]


def test_rule_defaults_are_empty():
    rule = FilterRule()
    assert rule.keywords == []
    assert rule.exclude_keywords == []
    assert rule.min_price is None
    assert rule.max_price is None
    assert passes(make_item("anything", 123), rule)


def test_no_price_item_passes_rule_without_bounds():
    rule = FilterRule(keywords="Built to Spill")
    assert passes(make_item("Built to Spill - CD", price=None), rule)
