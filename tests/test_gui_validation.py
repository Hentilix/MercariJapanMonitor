"""Offline tests for the GUI form validation/parsing helpers (main.py).

No NiceGUI interaction — the helpers are pure functions. Importing main is
offline-safe (it only defines globals; ui.run() lives in the guarded block).
"""

import pytest

import main
from main import (
    calculate_total_pages,
    clamp_page,
    config_status_text,
    deepseek_status_text,
    monitor_fields_from_form,
    parse_price,
    smtp_status_text,
    validate_monitor_form,
)


# ------------------------------------------------------------------ 测试 1/3/4/5/6
def test_valid_form_passes():
    assert (
        validate_monitor_form("A", "Built to Spill，CD", None, 5000, "AND", "只要 CD")
        is None
    )
    assert (
        validate_monitor_form("A", "Built to Spill, CD", 100, 5000, "OR", "req")
        is None
    )


def test_empty_name_blocked():
    error = validate_monitor_form("  ", "kw", None, None, "AND", "req")
    assert error is not None and "名称" in error


def test_empty_keywords_blocked():
    error = validate_monitor_form("A", "  ", None, None, "AND", "req")
    assert error is not None and "关键词" in error


def test_empty_ai_requirement_blocked():
    error = validate_monitor_form("A", "kw", None, None, "AND", "   ")
    assert error is not None and "AI" in error


def test_invalid_mode_blocked():
    error = validate_monitor_form("A", "kw", None, None, "XOR", "req")
    assert error is not None and "AND" in error


@pytest.mark.parametrize("bad_price", ["abc", "1.5", "1000.5", "-100", -100, 1.5])
def test_invalid_prices_blocked(bad_price):
    error = validate_monitor_form("A", "kw", bad_price, None, "AND", "req")
    assert error is not None and ("整数" in error or "负数" in error)


def test_min_greater_than_max_blocked():
    error = validate_monitor_form("A", "kw", 5000, 1000, "AND", "req")
    assert error is not None and "不能高于" in error


def test_min_equal_max_allowed():
    assert (
        validate_monitor_form("A", "kw", 1000, 1000, "AND", "req") is None
    )


# ---------------------------------------------------------------- parse_price
def test_parse_price_conversions():
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("   ") is None
    assert parse_price("1000") == 1000
    assert parse_price(1000) == 1000
    assert parse_price(1000.0) == 1000


@pytest.mark.parametrize("bad", ["abc", "1.5", "-100", -100, 1.5, "1000.5"])
def test_parse_price_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_price(bad)


# -------------------------------------------------------------- 字段转换 (46/47)
def test_fields_conversion_blank_to_none():
    fields = monitor_fields_from_form(
        " A ", " Built to Spill，CD ", "", 5000, "AND", " LP，DVD ",
        "只要 CD", 30, "  ", True,
    )
    assert fields["name"] == "A"
    assert fields["keywords"] == "Built to Spill，CD"  # raw string kept
    assert fields["min_price"] is None                 # "" -> None
    assert fields["max_price"] == 5000
    assert fields["filter_words"] == "LP，DVD"
    assert fields["ai_requirement"] == "只要 CD"
    assert fields["notification_email"] is None        # blank -> use SMTP_TO
    assert fields["enabled"] is True


def test_fields_conversion_keeps_email_when_given():
    fields = monitor_fields_from_form(
        "A", "kw", None, None, "AND", "", "req", 30, "chccrimson@gmail.com", False
    )
    assert fields["notification_email"] == "chccrimson@gmail.com"
    assert fields["enabled"] is False


# ---------------------------------------------------------- 重复名称 (测试 2)
def test_duplicate_name_raises_integrity_error(tmp_path):
    """The DB enforces uniqueness; the GUI converts it to a friendly message."""
    import sqlite3

    from app.database import Database

    db = Database(tmp_path / "test.db")
    db.create_monitor(name="A", keywords="kw", ai_requirement="req")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_monitor(name="A", keywords="kw2", ai_requirement="req")
    db.close()


# ------------------------------------------------------ 编辑历史不受影响 (12/27)
def test_editing_rules_keeps_history(tmp_path):
    """Updating a monitor's filter rules never touches monitor_products."""
    from app.database import Database

    db = Database(tmp_path / "test.db")
    mid = db.create_monitor(
        name="A", keywords="kw", min_price=None, max_price=5000, ai_requirement="req"
    )
    db.add_monitor_product(
        mid, mercari_id="mX", title="4500 JPY item", price=4500, url="https://jp.mercari.com/item/mX"
    )
    db.update_monitor(
        mid,
        name="A", keywords="kw", keyword_mode="AND", min_price=None,
        max_price=3000, filter_words="", ai_requirement="req",
        interval_minutes=30, notification_email=None, enabled=True,
    )
    rows = db.list_monitor_products(mid)
    assert len(rows) == 1 and rows[0][2] == 4500  # history untouched
    db.close()


# ------------------------------------------- 历史分页纯函数 (Phase 5.4-B 44/45)
def test_calculate_total_pages():
    assert calculate_total_pages(0, 20) == 1
    assert calculate_total_pages(1, 20) == 1
    assert calculate_total_pages(20, 20) == 1
    assert calculate_total_pages(21, 20) == 2
    assert calculate_total_pages(53, 20) == 3
    assert calculate_total_pages(40, 20) == 2


def test_clamp_page():
    assert clamp_page(5, 4) == 4    # beyond last page -> auto adjust
    assert clamp_page(0, 4) == 1
    assert clamp_page(-3, 4) == 1
    assert clamp_page(2, 4) == 2


def test_history_page_boundary_after_delete():
    """21 items -> page 2/2; deleting the only item on page 2 -> page 1/1."""
    total, page_size = 21, 20
    pages = calculate_total_pages(total, page_size)
    assert pages == 2
    assert clamp_page(2, pages) == 2
    pages_after = calculate_total_pages(total - 1, page_size)
    assert pages_after == 1


# ------------------------------------------- 配置状态行 (P1-3)
SMTP_ALL = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
            "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"]


@pytest.fixture
def clean_config_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY_FOR_MJM", raising=False)
    for key in SMTP_ALL:
        monkeypatch.delenv(key, raising=False)


def test_deepseek_status_unset(clean_config_env):
    assert deepseek_status_text() == "DeepSeek：未设置"


def test_deepseek_status_set(clean_config_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "sk-test")
    assert deepseek_status_text() == "DeepSeek：已设置"


def test_smtp_status_not_configured(clean_config_env):
    assert smtp_status_text() == "SMTP：未配置"


def test_smtp_status_configured(clean_config_env, monkeypatch):
    for key in SMTP_ALL:
        monkeypatch.setenv(key, "x")
    assert smtp_status_text() == "SMTP：已配置"


def test_smtp_status_partial_lists_missing_in_fixed_order(clean_config_env, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.qq.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    text = smtp_status_text()
    assert text.startswith("SMTP：配置不完整")
    for missing in ("SMTP_PORT", "SMTP_USERNAME", "SMTP_FROM", "SMTP_TO"):
        assert missing in text
    assert "SMTP_HOST" not in text.split("缺：", 1)[1]
    assert "SMTP_PASSWORD" not in text.split("缺：", 1)[1]


def test_config_status_line_combines_both(clean_config_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "sk-test")
    text = config_status_text()
    assert "DeepSeek：已设置" in text
    assert "SMTP：未配置" in text


# ------------------------------------------- mercapi 可选字段警告过滤
def test_mercapi_optional_field_warning_is_filtered():
    import logging

    from main import _MercapiOptionalFieldFilter

    log_filter = _MercapiOptionalFieldFilter()
    record = logging.LogRecord(
        "root", logging.WARNING, __file__, 1,
        "Encountered optional response property brand that could not be parsed correctly.",
        None, None,
    )
    assert log_filter.filter(record) is False


def test_other_log_records_pass_the_filter():
    import logging

    from main import _MercapiOptionalFieldFilter

    log_filter = _MercapiOptionalFieldFilter()
    record = logging.LogRecord(
        "root", logging.INFO, __file__, 1, "Monitor 1 扫描完成", None, None
    )
    assert log_filter.filter(record) is True


# ------------------------------------------- 历史页过滤选项 (方案 A)
def test_history_filter_values_mapping():
    from main import HISTORY_FILTER_VALUES

    assert HISTORY_FILTER_VALUES == {"matched": 1, "rejected": 0, "all": None}
