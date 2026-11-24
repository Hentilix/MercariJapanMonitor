"""Offline tests for the monitors table (Phase 5.2 schema) + migrations."""

import sqlite3
from pathlib import Path

import pytest

from app.database import Database, Monitor, split_keywords
from app.migrations import _migrate_monitors, migrate

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
    path = tmp_path / "test.db"
    db = Database(path)
    try:
        monitor_id = db.create_monitor(**FIELDS)
        snapshot = db.get_monitor(monitor_id)
        with pytest.raises(sqlite3.IntegrityError):
            db.create_monitor(name=FIELDS["name"], keywords="duplicate")
        assert db.update_monitor(
            monitor_id, **{**FIELDS, "name": "Renamed", "interval_minutes": 60},
            expected=snapshot,
        )
    finally:
        db.close()

    db = Database(path)
    try:
        mon, = db.list_monitors()
        assert (mon.id, mon.name, mon.keywords, mon.interval_minutes) == (
            monitor_id, "Renamed", FIELDS["keywords"], 60,
        )
    finally:
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
    assert "products" not in tables  # Fresh databases only get the current schema.
    monitor_columns = {r[1] for r in conn.execute("PRAGMA table_info(monitors)")}
    assert "keywords" in monitor_columns
    assert "keyword_mode" in monitor_columns
    assert "filter_words" in monitor_columns
    assert "ai_requirement" in monitor_columns
    assert "notification_email" in monitor_columns
    assert "query" not in monitor_columns  # old schema fully replaced
    assert "exclude_keywords" not in monitor_columns
    db.close()


def test_legacy_products_table_is_preserved_on_open(tmp_path):
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
    legacy_rows = [
        ("mX", "old item", 100, "u", "2026-09-15T10:00:00+08:00"),
        ("mY", "item without a price", None, "v", "2026-09-16T10:00:00+08:00"),
    ]
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", legacy_rows)
    schema_query = "SELECT sql FROM sqlite_schema WHERE type='table' AND name='products'"
    original_definition = conn.execute(schema_query).fetchone()
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db._conn.execute(schema_query).fetchone() == original_definition
        monitor_id = db.create_monitor(name="CD", keywords="CD")
    finally:
        db.close()

    db = Database(path)
    try:
        assert db._conn.execute(schema_query).fetchone() == original_definition
        assert db._conn.execute("SELECT * FROM products ORDER BY mercari_id").fetchall() == legacy_rows
        assert db.list_monitor_products(monitor_id, matched=None) == []
        assert db.product_decision(monitor_id, "mX") is None
        assert db.mail_status(monitor_id).pending == 0
    finally:
        db.close()


def test_readme_lists_every_current_table_and_column(tmp_path):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    schema_section = readme.split("## 数据模型\n", 1)[1].split("\n## ", 1)[0]
    documented = {}
    for line in schema_section.splitlines():
        if line.startswith("| `"):
            table, columns, _purpose = (part.strip() for part in line.strip("|").split("|"))
            documented[table.strip("`")] = {column.strip().strip("`") for column in columns.split(",")}

    db = Database(tmp_path / "schema.db")
    try:
        actual = {}
        for (table,) in db._conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        ):
            actual[table] = {
                column for (column,) in db._conn.execute("SELECT name FROM pragma_table_info(?)", (table,))
            }
        assert documented == actual
    finally:
        db.close()


@pytest.fixture
def legacy_monitors_path(tmp_path):
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
    return path


def test_old_monitors_table_is_migrated_with_data(legacy_monitors_path):
    """A pre-5.2 monitors table gets rebuilt with mapped data, nothing lost."""
    db = Database(legacy_monitors_path)
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


@pytest.fixture
def staging_definition():
    conn = sqlite3.connect(":memory:")
    try:
        # Capture the staging shape before later upgrades add columns.
        _migrate_monitors(conn)
        definition, = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name='monitors'"
        ).fetchone()
    finally:
        conn.close()
    return definition.replace("CREATE TABLE monitors", "CREATE TABLE monitors_new", 1)


def test_duplicate_migration_recovers_empty_staging_and_preserves_links(
    legacy_monitors_path, staging_definition,
):
    with sqlite3.connect(legacy_monitors_path) as conn:
        conn.execute(staging_definition)
        conn.executemany(
            """INSERT INTO monitors (id, name, query, created_at, updated_at)
               VALUES (?, ?, ?, 'created', 'updated')""",
            [(5, "Test", "second"), (9, "Test (2)", "existing"), (30, "deleted", "")],
        )
        conn.execute("DELETE FROM monitors WHERE id=30")
        conn.execute(
            "CREATE TABLE saved_items (monitor_id REFERENCES monitors(id) ON DELETE CASCADE, item TEXT)"
        )
        conn.execute("INSERT INTO saved_items VALUES (5, 'listing')")
    conn.close()

    expected = [(1, "Test"), (5, "Test (3)"), (9, "Test (2)")]
    for _ in range(2):
        db = Database(legacy_monitors_path)
        assert [(mon.id, mon.name) for mon in db.list_monitors()] == expected
        assert db.get_monitor(5).keywords == "second"
        assert db.get_monitor(9).keywords == "existing"
        assert db._conn.execute("SELECT * FROM saved_items").fetchall() == [(5, "listing")]
        assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db._conn.execute("SELECT name FROM sqlite_schema WHERE name='monitors_new'").fetchone() is None
        db.close()

    db = Database(legacy_monitors_path)
    assert db.create_monitor(name="New", keywords="new") == 31
    db.delete_monitor(5)
    assert db._conn.execute("SELECT * FROM saved_items").fetchall() == []
    db.close()


@pytest.mark.parametrize("problem", ["data", "schema"])
def test_monitor_migration_preserves_unexpected_staging(legacy_monitors_path, staging_definition, problem):
    if problem == "schema":
        staging_definition = "CREATE TABLE monitors_new (id INTEGER PRIMARY KEY)"
    with sqlite3.connect(legacy_monitors_path) as conn:
        conn.execute(staging_definition)
        if problem == "data":
            conn.execute(
                """INSERT INTO monitors_new
                       (name, keywords, keyword_mode, ai_requirement, interval_minutes, created_at, updated_at)
                   VALUES ('Saved', 'CD', 'AND', '', 30, 'created', 'updated')"""
            )
        original_rows = conn.execute("SELECT * FROM monitors_new").fetchall()
    conn.close()

    with pytest.raises(sqlite3.OperationalError, match=f"({problem}).*monitors_new|monitors_new.*({problem})"):
        Database(legacy_monitors_path)
    conn = sqlite3.connect(legacy_monitors_path)
    try:
        assert conn.execute("SELECT * FROM monitors_new").fetchall() == original_rows
        assert conn.execute("SELECT name, query FROM monitors WHERE id=1").fetchone()[0] == "Test"
    finally:
        conn.close()


def test_monitor_migration_rolls_back_failed_table_swap(legacy_monitors_path):
    db = Database.__new__(Database)
    db._conn = sqlite3.connect(legacy_monitors_path)
    try:
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.execute("CREATE TABLE saved_items (monitor_id REFERENCES monitors(id) ON DELETE CASCADE)")
        db._conn.execute("INSERT INTO saved_items VALUES (1)")
        db._conn.commit()
        db._conn.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK
        )
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            migrate(db._conn)
        db._conn.set_authorizer(None)
        assert db._conn.execute("SELECT id FROM monitors").fetchall() == [(1,)]
        assert db._conn.execute("SELECT * FROM saved_items").fetchall() == [(1,)]
        assert db._conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert db._conn.execute("SELECT name FROM sqlite_schema WHERE name='monitors_new'").fetchone() is None
    finally:
        db.close()

    db = Database(legacy_monitors_path)
    assert db.get_monitor(1).name == "Test"
    assert db._conn.execute("SELECT * FROM saved_items").fetchall() == [(1,)]
    db.close()
