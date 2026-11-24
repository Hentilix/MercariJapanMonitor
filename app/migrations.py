"""Create or upgrade the SQLite schema on the database connection."""

import sqlite3


_MONITORS_SCHEMA = """
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        keywords TEXT NOT NULL,
        keyword_mode TEXT NOT NULL,
        min_price INTEGER,
        max_price INTEGER,
        filter_words TEXT,
        ai_requirement TEXT NOT NULL,
        interval_minutes INTEGER NOT NULL,
        notification_email TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_scan_at TEXT,
        last_result TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Apply the existing schema upgrades to an open connection."""
    _migrate_monitors(conn)
    monitor_columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
    if "rule_version" not in monitor_columns:
        conn.execute(
            "ALTER TABLE monitors ADD COLUMN rule_version INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            mercari_id TEXT NOT NULL,
            title TEXT NOT NULL,
            price INTEGER,
            published_at TEXT,
            url TEXT NOT NULL,
            found_at TEXT NOT NULL,
            matched INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE,
            UNIQUE (monitor_id, mercari_id)
        )
        """
    )
    # Existing decisions predate rule versions. NULL preserves their unknown version.
    mp_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(monitor_products)")
    }
    if "matched" not in mp_columns:
        conn.execute(
            "ALTER TABLE monitor_products ADD COLUMN matched INTEGER NOT NULL DEFAULT 1"
        )
    if "judged_rule_version" not in mp_columns:
        conn.execute(
            "ALTER TABLE monitor_products ADD COLUMN judged_rule_version INTEGER"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ignored_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            mercari_id TEXT NOT NULL,
            title TEXT,
            price INTEGER,
            url TEXT,
            ignored_at TEXT NOT NULL,
            FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE,
            UNIQUE (monitor_id, mercari_id)
        )
        """
    )
    # Phase 5.4-C: snapshot title/price/url at ignore time so the
    # ignored list can be displayed without re-querying Mercari.
    ig_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ignored_products)")
    }
    for column, column_type in (("title", "TEXT"), ("price", "INTEGER"), ("url", "TEXT")):
        if column not in ig_columns:
            conn.execute(
                f"ALTER TABLE ignored_products ADD COLUMN {column} {column_type}"
            )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_batches (
            message_id TEXT PRIMARY KEY NOT NULL,
            monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            recipient TEXT NOT NULL,
            next_attempt_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            failed_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_items (
            message_id TEXT NOT NULL REFERENCES mail_batches(message_id) ON DELETE CASCADE,
            mercari_id TEXT NOT NULL,
            title TEXT NOT NULL,
            price INTEGER,
            url TEXT NOT NULL,
            UNIQUE (message_id, mercari_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_progress (
            monitor_id INTEGER PRIMARY KEY REFERENCES monitors(id) ON DELETE CASCADE,
            generation TEXT NOT NULL,
            state TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_candidates (
            id INTEGER PRIMARY KEY,
            monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            mercari_id TEXT NOT NULL,
            title TEXT NOT NULL,
            price INTEGER,
            url TEXT NOT NULL,
            item_type TEXT,
            published_at TEXT,
            created_epoch REAL,
            retry_at REAL NOT NULL DEFAULT 0,
            detail_json TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (monitor_id, mercari_id)
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_candidates)")}
    if "detail_json" not in columns:
        conn.execute("ALTER TABLE scan_candidates ADD COLUMN detail_json TEXT")
    if "failure_count" not in columns:
        with conn:
            # Start a transaction even when the preceding migrations ran only DDL.
            conn.execute("SAVEPOINT candidate_failure_count")
            conn.execute(
                "ALTER TABLE scan_candidates ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE scan_candidates SET failure_count=1 WHERE retry_at>0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_rechecks (
            monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            mercari_id TEXT NOT NULL,
            PRIMARY KEY (monitor_id, mercari_id)
        )
    """)
    conn.commit()


def _migrate_monitors(conn: sqlite3.Connection) -> None:
    """Create the current monitor table or rebuild the legacy schema."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
    if not columns:
        conn.execute(_MONITORS_SCHEMA.format(table="monitors"))
        return
    if "keywords" in columns:
        return

    foreign_keys, = conn.execute("PRAGMA foreign_keys").fetchone()
    # SQLite cascades a parent DROP while foreign keys are enabled.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # Recheck after acquiring the write lock in case another opener migrated.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
            if "keywords" in columns:
                return
            _rebuild_legacy_monitors(conn)
            violation = conn.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise sqlite3.IntegrityError(f"Foreign-key violation after monitor migration: {violation}")
    finally:
        conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def _rebuild_legacy_monitors(conn: sqlite3.Connection) -> None:
    schema = _MONITORS_SCHEMA.format(table="monitors_new")
    staging_objects = conn.execute(
        """SELECT type, name, sql FROM sqlite_schema
           WHERE name='monitors_new' OR (tbl_name='monitors_new' AND sql IS NOT NULL)"""
    ).fetchall()
    if staging_objects:
        if len(staging_objects) != 1 or staging_objects[0][:2] != ("table", "monitors_new"):
            raise sqlite3.OperationalError("Monitor migration found an unexpected monitors_new schema")
        definition = staging_objects[0][2]
        if definition is None or definition.split() != schema.split():
            raise sqlite3.OperationalError("Monitor migration found an unexpected monitors_new schema")
        if conn.execute("SELECT 1 FROM monitors_new LIMIT 1").fetchone() is not None:
            raise sqlite3.OperationalError("Monitor migration found existing data in monitors_new")
        conn.execute("DROP TABLE monitors_new")
    conn.execute(schema)

    rows = conn.execute(
        """SELECT id, name, query, COALESCE(match_mode, 'AND'),
                  min_price, max_price, COALESCE(exclude_keywords, ''),
                  COALESCE(special_requirement, ''), interval_minutes,
                  email_to, enabled, last_scan_at, last_result, created_at, updated_at
           FROM monitors ORDER BY id"""
    ).fetchall()
    reserved_names = {row[1] for row in rows}
    next_suffix = {}
    migrated_rows = []
    for monitor_id, name, *fields in rows:
        original_name = name
        suffix = next_suffix.get(original_name, 1)
        if suffix > 1:
            name = f"{original_name} ({suffix})"
            while name in reserved_names:
                suffix += 1
                name = f"{original_name} ({suffix})"
            reserved_names.add(name)
        next_suffix[original_name] = suffix + 1
        migrated_rows.append((monitor_id, name, *fields))
    conn.executemany(
        """INSERT INTO monitors_new
               (id, name, keywords, keyword_mode, min_price, max_price,
                filter_words, ai_requirement, interval_minutes,
                notification_email, enabled, last_scan_at, last_result,
                created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        migrated_rows,
    )
    old_sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='monitors'").fetchone()
    if old_sequence is not None:
        previous_max, = old_sequence
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='monitors_new'",
            (previous_max,),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES ('monitors_new', ?)",
                (previous_max,),
            )
    conn.execute("DROP TABLE monitors")
    conn.execute("ALTER TABLE monitors_new RENAME TO monitors")
