"""SQLite persistence.

Standard library sqlite3 only. No ORM, no async, no connection pool.

Final schema (per-monitor processing state):
    monitors          — one row per monitor task (name UNIQUE)
    monitor_products  — items a monitor has fully processed and matched
                        (Level 1 + DeepSeek true), UNIQUE(monitor_id, mercari_id)
    ignored_products  — items the user told a monitor to ignore forever,
                        UNIQUE(monitor_id, mercari_id)

A monitor's processing state is per-monitor: the same Mercari item can be
matched by several monitors independently.

The legacy global `products` table (Phase 2/3) is dropped on startup
(Phase 5.4-D); its data is intentionally NOT migrated because it has no
reliable monitor_id.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Anchored to the project directory, independent of the current working dir.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "mercari_monitor.db"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def split_keywords(text: str | None) -> list[str]:
    """Parse keywords separated by Chinese commas (，).

    ASCII commas are also accepted for backward compatibility.
    """
    if not text:
        return []
    text = text.replace("，", ",")
    return [k.strip() for k in text.split(",") if k.strip()]


# Fixed ORDER BY fragments for the history list — never built from user
# input. NULLs always sort last so they never disturb the ordering.
PRODUCT_SORT_OPTIONS = {
    "price_desc": "price IS NULL, price DESC, found_at DESC",
    "price_asc": "price IS NULL, price ASC, found_at DESC",
    "published_desc": "published_at IS NULL, published_at DESC, found_at DESC",
    "published_asc": "published_at IS NULL, published_at ASC, found_at DESC",
}


@dataclass
class Monitor:
    id: int
    name: str
    keywords: str            # raw user string, split via split_keywords()
    keyword_mode: str        # "AND" | "OR"
    min_price: int | None
    max_price: int | None
    filter_words: str        # raw user string; any hit rejects the item
    ai_requirement: str      # passed to DeepSeek
    interval_minutes: int
    notification_email: str | None  # empty -> SMTP_TO env var
    enabled: bool
    last_scan_at: str | None
    last_result: str | None
    created_at: str
    updated_at: str


_MONITOR_COLUMNS = (
    "id, name, keywords, keyword_mode, min_price, max_price, filter_words, "
    "ai_requirement, interval_minutes, notification_email, enabled, "
    "last_scan_at, last_result, created_at, updated_at"
)


class Database:
    """A single connection to the SQLite file."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        # Required for ON DELETE CASCADE to actually fire.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # ------------------------------------------------------------------ schema
    def _migrate(self) -> None:
        self._migrate_monitors()
        self._drop_legacy_products()
        self._conn.execute(
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
        # Phase 5.3-A: 'matched' distinguishes history items (matched=1,
        # DeepSeek true) from processed-but-rejected items (matched=0,
        # DeepSeek false — never shown in the history, never re-judged).
        mp_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(monitor_products)")
        }
        if "matched" not in mp_columns:
            self._conn.execute(
                "ALTER TABLE monitor_products ADD COLUMN matched INTEGER NOT NULL DEFAULT 1"
            )
        self._conn.execute(
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
            row[1] for row in self._conn.execute("PRAGMA table_info(ignored_products)")
        }
        for column, column_type in (("title", "TEXT"), ("price", "INTEGER"), ("url", "TEXT")):
            if column not in ig_columns:
                self._conn.execute(
                    f"ALTER TABLE ignored_products ADD COLUMN {column} {column_type}"
                )
        self._conn.commit()

    def _drop_legacy_products(self) -> None:
        """Phase 5.4-D: retire the legacy global products table.

        Idempotent one-time migration: drops the table when it still
        exists and does nothing afterwards. Its data is NOT migrated to
        monitor_products — the old rows have no reliable monitor_id.
        The new tables are created before this runs and are never touched.
        """
        self._conn.execute("DROP TABLE IF EXISTS products")

    def _migrate_monitors(self) -> None:
        """Create the Phase 5.2 monitors schema.

        An existing pre-5.2 table (columns query/exclude_keywords/...) is
        rebuilt in place with its data mapped to the new column names.
        Nothing is deleted.
        """
        exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='monitors'"
        ).fetchone()
        if exists is None:
            self._conn.execute(
                """
                CREATE TABLE monitors (
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
            )
            return

        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(monitors)")}
        if "keywords" in columns:
            return  # already on the new schema

        self._conn.execute(
            """
            CREATE TABLE monitors_new (
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
        )
        self._conn.execute(
            """
            INSERT INTO monitors_new
                (id, name, keywords, keyword_mode, min_price, max_price,
                 filter_words, ai_requirement, interval_minutes,
                 notification_email, enabled, last_scan_at, last_result,
                 created_at, updated_at)
            SELECT id, name, query, COALESCE(match_mode, 'AND'),
                   min_price, max_price, COALESCE(exclude_keywords, ''),
                   COALESCE(special_requirement, ''), interval_minutes,
                   email_to, enabled, last_scan_at, last_result,
                   created_at, updated_at
            FROM monitors
            """
        )
        self._conn.execute("DROP TABLE monitors")
        self._conn.execute("ALTER TABLE monitors_new RENAME TO monitors")

    # ---------------------------------------------------- monitors (5.2)
    def create_monitor(
        self,
        *,
        name: str,
        keywords: str,
        keyword_mode: str = "AND",
        min_price: int | None = None,
        max_price: int | None = None,
        filter_words: str = "",
        ai_requirement: str = "",
        interval_minutes: int = 30,
        notification_email: str | None = None,
        enabled: bool = True,
    ) -> int:
        """Create a monitor. Raises sqlite3.IntegrityError on duplicate name."""
        now = _now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO monitors
                (name, keywords, keyword_mode, min_price, max_price,
                 filter_words, ai_requirement, interval_minutes,
                 notification_email, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                keywords,
                keyword_mode,
                min_price,
                max_price,
                filter_words,
                ai_requirement,
                interval_minutes,
                notification_email,
                int(enabled),
                now,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_monitor(self, monitor_id: int) -> Monitor | None:
        row = self._conn.execute(
            f"SELECT {_MONITOR_COLUMNS} FROM monitors WHERE id=?", (monitor_id,)
        ).fetchone()
        return self._row_to_monitor(row) if row else None

    def get_monitor_by_name(self, name: str) -> Monitor | None:
        row = self._conn.execute(
            f"SELECT {_MONITOR_COLUMNS} FROM monitors WHERE name=?", (name,)
        ).fetchone()
        return self._row_to_monitor(row) if row else None

    def list_monitors(self) -> list[Monitor]:
        rows = self._conn.execute(
            f"SELECT {_MONITOR_COLUMNS} FROM monitors ORDER BY id"
        ).fetchall()
        return [self._row_to_monitor(row) for row in rows]

    def update_monitor(
        self,
        monitor_id: int,
        *,
        name: str,
        keywords: str,
        keyword_mode: str,
        min_price: int | None,
        max_price: int | None,
        filter_words: str,
        ai_requirement: str,
        interval_minutes: int,
        notification_email: str | None,
        enabled: bool,
    ) -> None:
        self._conn.execute(
            """
            UPDATE monitors SET name=?, keywords=?, keyword_mode=?,
                min_price=?, max_price=?, filter_words=?, ai_requirement=?,
                interval_minutes=?, notification_email=?, enabled=?,
                updated_at=?
            WHERE id=?
            """,
            (
                name,
                keywords,
                keyword_mode,
                min_price,
                max_price,
                filter_words,
                ai_requirement,
                interval_minutes,
                notification_email,
                int(enabled),
                _now_iso(),
                monitor_id,
            ),
        )
        self._conn.commit()

    def set_monitor_enabled(self, monitor_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE monitors SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), _now_iso(), monitor_id),
        )
        self._conn.commit()

    def set_last_scan(self, monitor_id: int, result: str) -> None:
        self._conn.execute(
            "UPDATE monitors SET last_scan_at=?, last_result=? WHERE id=?",
            (_now_iso(), result, monitor_id),
        )
        self._conn.commit()

    def delete_monitor(self, monitor_id: int) -> None:
        """Delete a monitor; its products and ignored entries cascade."""
        self._conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        self._conn.commit()

    @staticmethod
    def _row_to_monitor(row: tuple) -> Monitor:
        return Monitor(
            id=row[0],
            name=row[1],
            keywords=row[2],
            keyword_mode=row[3],
            min_price=row[4],
            max_price=row[5],
            filter_words=row[6],
            ai_requirement=row[7],
            interval_minutes=row[8],
            notification_email=row[9],
            enabled=bool(row[10]),
            last_scan_at=row[11],
            last_result=row[12],
            created_at=row[13],
            updated_at=row[14],
        )

    # ------------------------------------------ monitor products (5.2)
    # A row means "this monitor fully processed this item". matched=1 rows
    # (DeepSeek true) form the visible history; matched=0 rows (DeepSeek
    # false) mark the item as processed without entering the history.
    def has_monitor_product(self, monitor_id: int, mercari_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM monitor_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).fetchone()
        return row is not None

    def add_monitor_product(
        self,
        monitor_id: int,
        *,
        mercari_id: str,
        title: str,
        price: int | None,
        url: str,
        published_at: str | None = None,
        matched: bool = True,
    ) -> None:
        """Record a fully processed item for a monitor.

        matched=True  -> DeepSeek true, enters the history list
        matched=False -> DeepSeek false, processed but never re-judged,
                         never shown in the history
        Duplicate (monitor_id, mercari_id) inserts are ignored. published_at
        is the Mercari listing's own publish time (from the search result's
        `created` field); it stays NULL when unavailable.
        """
        self._conn.execute(
            """
            INSERT OR IGNORE INTO monitor_products
                (monitor_id, mercari_id, title, price, published_at, url,
                 found_at, matched)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                monitor_id,
                mercari_id,
                title,
                price,
                published_at,
                url,
                _now_iso(),
                int(matched),
            ),
        )
        self._conn.commit()

    def list_monitor_products(
        self,
        monitor_id: int,
        *,
        sort_by: str = "published_desc",
        limit: int = 20,
        offset: int = 0,
        matched: int | None = 1,
    ) -> list[tuple]:
        """History page rows for one monitor.

        (mercari_id, title, price, url, published_at, found_at)

        matched=1 -> only DeepSeek-true items (default, the visible
        history); matched=0 -> only processed-but-rejected items;
        matched=None -> both.

        sort_by must be one of PRODUCT_SORT_OPTIONS; anything else falls
        back to published_desc. NULL prices/timestamps sort last so they
        never break the ordering. limit/offset are clamped (limit > 0,
        offset >= 0).
        """
        order_clause = PRODUCT_SORT_OPTIONS.get(sort_by, PRODUCT_SORT_OPTIONS["published_desc"])
        if limit <= 0:
            limit = 20
        if offset < 0:
            offset = 0
        if matched is None:
            where, args = "monitor_id=?", (monitor_id,)
        else:
            where, args = "monitor_id=? AND matched=?", (monitor_id, int(matched))
        return self._conn.execute(
            f"SELECT mercari_id, title, price, url, published_at, found_at "
            f"FROM monitor_products WHERE {where} "
            f"ORDER BY {order_clause} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()

    def count_monitor_products(
        self, monitor_id: int, matched: int | None = 1
    ) -> int:
        """Number of history rows for one monitor (matched filter like
        list_monitor_products: 1 / 0 / None=both)."""
        if matched is None:
            where, args = "monitor_id=?", (monitor_id,)
        else:
            where, args = "monitor_id=? AND matched=?", (monitor_id, int(matched))
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM monitor_products WHERE {where}", args
        ).fetchone()
        return row[0]

    def delete_monitor_product(self, monitor_id: int, mercari_id: str) -> None:
        """Remove an item from the monitor's history.

        This is NOT "ignore": the item may be re-processed on a later scan.
        """
        self._conn.execute(
            "DELETE FROM monitor_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        )
        self._conn.commit()

    # ------------------------------------------ ignored products (5.2)
    def is_ignored(self, monitor_id: int, mercari_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ignored_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).fetchone()
        return row is not None

    def ignore_product(
        self, monitor_id: int, mercari_id: str, at: str | None = None
    ) -> None:
        """Ignore an item for one monitor: remove it from the history and
        add it to the ignored list (never both at the same time).

        The title/price/url snapshot is copied from the history row before
        it is deleted, so the ignored list can be displayed later without
        any Mercari requests. Ignoring an item that is not in the history
        still records the ignored entry (fields become NULL/unknown).
        """
        row = self._conn.execute(
            "SELECT title, price, url FROM monitor_products "
            "WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).fetchone()
        title, price, url = row if row else (None, None, None)
        self._conn.execute(
            "DELETE FROM monitor_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ignored_products
                (monitor_id, mercari_id, title, price, url, ignored_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (monitor_id, mercari_id, title, price, url, at or _now_iso()),
        )
        self._conn.commit()

    def unignore_product(self, monitor_id: int, mercari_id: str) -> None:
        """Only removes the ignored entry — does NOT re-add to the history.

        The item becomes eligible for processing again on the next scan.
        Missing records are a no-op.
        """
        self._conn.execute(
            "DELETE FROM ignored_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        )
        self._conn.commit()

    def list_ignored_products(
        self, monitor_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[tuple]:
        """Ignored list for one monitor, newest ignore first.

        (mercari_id, title, price, url, ignored_at). limit/offset are
        clamped (limit > 0, offset >= 0).
        """
        if limit <= 0:
            limit = 20
        if offset < 0:
            offset = 0
        return self._conn.execute(
            "SELECT mercari_id, title, price, url, ignored_at "
            "FROM ignored_products WHERE monitor_id=? "
            "ORDER BY ignored_at DESC, id DESC LIMIT ? OFFSET ?",
            (monitor_id, limit, offset),
        ).fetchall()

    def count_ignored_products(self, monitor_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM ignored_products WHERE monitor_id=?",
            (monitor_id,),
        ).fetchone()
        return row[0]

    def close(self) -> None:
        self._conn.close()
