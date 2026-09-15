"""Level 1 deterministic hard filters.

Pure rules only: keyword / min price / max price / exclude keywords.
No AI, no NLP, no network.
"""

from dataclasses import dataclass

from app.mercari import SearchItem


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


@dataclass
class FilterRule:
    """A Level 1 rule set. All conditions must pass (AND semantics).

    match_mode controls how the positive keywords combine:
      "AND" (default): title must contain every keyword
      "OR"           : title must contain at least one keyword
    Exclude keywords are always OR-like: any hit rejects the item.
    """

    keywords: str | list[str] | None = None
    exclude_keywords: str | list[str] | None = None
    min_price: int | None = None
    max_price: int | None = None
    match_mode: str = "AND"

    def __post_init__(self) -> None:
        self.keywords = _as_list(self.keywords)
        self.exclude_keywords = _as_list(self.exclude_keywords)
        self.match_mode = self.match_mode.upper()
        if self.match_mode not in ("AND", "OR"):
            raise ValueError(f"match_mode must be 'AND' or 'OR', got {self.match_mode!r}")


def matches_keyword(
    title: str | None, keywords: list[str], mode: str = "AND"
) -> bool:
    """AND: title contains every keyword; OR: title contains any keyword.

    Matching is case-insensitive (casefold) substring matching.
    """
    if not keywords:
        return True
    if not title:
        return False
    folded = title.casefold()
    folded_keywords = [k.casefold() for k in keywords]
    if mode == "OR":
        return any(k in folded for k in folded_keywords)
    return all(k in folded for k in folded_keywords)


def matches_exclude(title: str | None, exclude_keywords: list[str]) -> bool:
    """True when the title is NOT hit by any exclude keyword."""
    if not exclude_keywords or not title:
        return True
    folded = title.casefold()
    return not any(k.casefold() in folded for k in exclude_keywords)


def matches_price(
    price: int | None, min_price: int | None, max_price: int | None
) -> bool:
    """True when price is within the inclusive bounds.

    An unknown price (None) fails only when at least one bound is set.
    """
    if min_price is None and max_price is None:
        return True
    if price is None:
        return False
    if min_price is not None and price < min_price:
        return False
    if max_price is not None and price > max_price:
        return False
    return True


def passes(item: SearchItem, rule: FilterRule) -> bool:
    """Apply all Level 1 conditions to one item."""
    # Exclude first: an excluded item is rejected no matter what it matches.
    if not matches_exclude(item.title, rule.exclude_keywords):
        return False
    if not matches_keyword(item.title, rule.keywords, rule.match_mode):
        return False
    return matches_price(item.price, rule.min_price, rule.max_price)


def apply_filter(items: list[SearchItem], rule: FilterRule) -> list[SearchItem]:
    """Return the items that pass the Level 1 rule."""
    return [item for item in items if passes(item, rule)]
