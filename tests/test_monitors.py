"""Offline tests for the monitors table (Phase 5.2 schema) + migrations."""

import sqlite3

import pytest

from app.database import Database, Monitor, split_keywords

FIELDS = dict(
    name="Built to Spill CD",
    keywords="Built to Spill，CD",
    keyword_mode="AND",
    min_price=1000,
    max_price=5000,
    filter_words="LP，DVD，Blu-ray",
    ai_requirement="只要 CD，不要黑胶、DVD 和数字版",
    interval_minutes=30,
    notification_email="chccrimson@gmail.com",
    enabled=True,
)


# ------------------------------------------------------------- split_keywords
def test_split_keywords_normal():
    assert split_keywords("LP, DVD, Blu-ray") == ["LP", "DVD", "Blu-ray"]


def test_split_keywords_chinese_comma():
    assert split_keywords("LP，DVD，Blu-ray") == ["LP", "DVD", "Blu-ray"]
    assert split_keywords("John Coltrane，A Love Supreme，CD") == [
        "John Coltrane",
        "A Love Supreme",
        "CD",
    ]
    assert split_keywords("LP，DVD, Blu-ray") == ["LP", "DVD", "Blu-ray"]  # mixed


def test_split_keywords_edge_cases():
    assert split_keywords("") == []
    assert split_keywords(None) == []
    assert split_keywords("LP，，DVD ，") == ["LP", "DVD"]
    assert split_keywords("LP") == ["LP"]


# ------------------------------------------------------------------- monitors
def test_create_and_get_monitor(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(**FIELDS)
    mon = db.get_monitor(monitor_id)
    assert isinstance(mon, Monitor)
    assert mon.id == monitor_id
    assert mon.name == FIELDS["name"]
    assert mon.keywords == FIELDS["keywords"]
    assert mon.keyword_mode == "AND"
    assert mon.min_price == 1000
    assert mon.max_price == 5000
    assert mon.filter_words == "LP，DVD，Blu-ray"
    assert mon.ai_requirement == FIELDS["ai_requirement"]
    assert mon.interval_minutes == 30
    assert mon.notification_email == "chccrimson@gmail.com"
    assert mon.enabled is True
    assert mon.last_scan_at is None
    db.close()


def test_create_monitor_defaults(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(name="N", keywords="Q")
    mon = db.get_monitor(monitor_id)
    assert mon.keyword_mode == "AND"
    assert mon.min_price is None
    assert mon.max_price is None
    assert mon.filter_words == ""
    assert mon.ai_requirement == ""
    assert mon.interval_minutes == 30
    assert mon.notification_email is None
    assert mon.enabled is True
    db.close()


def test_monitor_name_must_be_unique(tmp_path):
    db = Database(tmp_path / "test.db")
    db.create_monitor(name="Same", keywords="q1")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_monitor(name="Same", keywords="q2")
    db.close()


def test_get_monitor_by_name(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(**FIELDS)
    mon = db.get_monitor_by_name(FIELDS["name"])
    assert mon is not None and mon.id == monitor_id
    assert db.get_monitor_by_name("does not exist") is None
    db.close()


def test_list_monitors_ordered(tmp_path):
    db = Database(tmp_path / "test.db")
    db.create_monitor(name="A", keywords="a")
    db.create_monitor(name="B", keywords="b")
    assert [m.name for m in db.list_monitors()] == ["A", "B"]
    db.close()


def test_update_monitor(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(**FIELDS)
    db.update_monitor(
        monitor_id,
        **{**FIELDS, "name": "改名", "keyword_mode": "OR", "interval_minutes": 60, "enabled": False},
    )
    mon = db.get_monitor(monitor_id)
    assert mon.name == "改名"
    assert mon.keyword_mode == "OR"
    assert mon.interval_minutes == 60
    assert mon.enabled is False
    db.close()


def test_set_monitor_enabled(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(**FIELDS)
    db.set_monitor_enabled(monitor_id, False)
    assert db.get_monitor(monitor_id).enabled is False
    db.set_monitor_enabled(monitor_id, True)
    assert db.get_monitor(monitor_id).enabled is True
    db.close()


def test_delete_monitor(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(**FIELDS)
    db.delete_monitor(monitor_id)
    assert db.get_monitor(monitor_id) is None
    assert db.list_monitors() == []
    db.close()


def test_set_last_scan(tmp_path):
    db = Database(tmp_path / "test.db")
    monitor_id = db.create_monitor(**FIELDS)
    db.set_last_scan(monitor_id, "found=237, candidates=49, new=0")
    mon = db.get_monitor(monitor_id)
    assert mon.last_scan_at is not None
    assert mon.last_result == "found=237, candidates=49, new=0"
    db.close()


def test_monitors_persist_after_reopen(tmp_path):
    path = tmp_path / "test.db"
    db = Database(path)
    monitor_id = db.create_monitor(**FIELDS)
    db.close()
    reopened = Database(path)
    mon = reopened.get_monitor(monitor_id)
    assert mon is not None
    assert mon.name == FIELDS["name"]
    reopened.close()


# ----------------------------------------------------------------- migration
def test_migration_idempotent_and_complete(tmp_path):
    path = tmp_path / "test.db"
    Database(path).close()
    db = Database(path)  # second open must not fail
    conn = db._conn
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"monitors", "monitor_products", "ignored_products"} <= tables
    assert "products" not in tables  # legacy table fully retired (Phase 5.4-D)
    monitor_columns = {r[1] for r in conn.execute("PRAGMA table_info(monitors)")}
    assert "keywords" in monitor_columns
    assert "keyword_mode" in monitor_columns
    assert "filter_words" in monitor_columns
    assert "ai_requirement" in monitor_columns
    assert "notification_email" in monitor_columns
    assert "query" not in monitor_columns  # old schema fully replaced
    assert "exclude_keywords" not in monitor_columns
    db.close()


def test_legacy_products_table_is_dropped_on_open(tmp_path):
    """Phase 5.4-D: a leftover Phase 2 products table is dropped once,
    idempotently, without touching the new tables."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE products (
            mercari_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            price INTEGER,
            url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO products VALUES ('mX', 'old item', 100, 'u', '2026-09-15T10:00:00+08:00')"
    )
    conn.commit()
    conn.close()

    db = Database(path)  # migration runs here
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "products" not in tables                       # dropped with its data
    assert {"monitors", "monitor_products", "ignored_products"} <= tables
    db.close()

    Database(path).close()  # second open: nothing to drop, must not fail


def test_old_monitors_table_is_migrated_with_data(tmp_path):
    """A pre-5.2 monitors table gets rebuilt with mapped data, nothing lost."""
    path = tmp_path / "test.db"
    # Build an OLD-schema database by hand.
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            query TEXT NOT NULL,
            min_price INTEGER,
            max_price INTEGER,
            exclude_keywords TEXT NOT NULL DEFAULT '',
            match_mode TEXT NOT NULL DEFAULT 'AND',
            special_requirement TEXT,
            interval_minutes INTEGER NOT NULL DEFAULT 30,
            email_to TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_scan_at TEXT,
            last_result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO monitors (name, query, match_mode, min_price, max_price,
                              exclude_keywords, special_requirement,
                              interval_minutes, email_to, enabled,
                              created_at, updated_at)
        VALUES ('Test', 'John Coltrane, A Love Supreme, CD', 'AND', 1000, 3000,
                'LP', '要求日版首版', 5, 'chccrimson@gmail.com', 1,
                '2026-09-15T10:00:00+08:00', '2026-09-15T10:00:00+08:00')
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)  # migration runs here
    mon = db.get_monitor_by_name("Test")
    assert mon is not None
    assert mon.keywords == "John Coltrane, A Love Supreme, CD"
    assert mon.keyword_mode == "AND"
    assert mon.filter_words == "LP"
    assert mon.ai_requirement == "要求日版首版"
    assert mon.min_price == 1000 and mon.max_price == 3000
    assert mon.interval_minutes == 5
    assert mon.notification_email == "chccrimson@gmail.com"
    assert mon.enabled is True
    db.close()