"""SQLite persistence.

Standard library sqlite3 only. No ORM, no async, no connection pool.

Each monitor owns its listing decisions, ignored items, search progress,
pending candidates, rechecks and queued mail. Listing decisions include both
matches and rejections and store the applicable rule version when known.

Existing Phase 2/3 `products` tables are retained for reference. Their rows
have no reliable monitor ID, so monitor-specific data comes from newer tables.
"""

import sqlite3
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.migrations import migrate

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
    rule_version: int = 1

    def search_settings(self) -> tuple:
        return _search_settings(
            self.keywords, self.keyword_mode, self.min_price, self.max_price, self.filter_words,
        )

    def needs_judgment(self, decision: tuple[int, int | None] | None) -> bool:
        if decision is None:
            return True
        matched, version = decision
        return not matched and version != self.rule_version

    def editable_settings(self) -> tuple:
        return (
            self.name, self.keywords, self.keyword_mode, self.min_price, self.max_price,
            self.filter_words, self.ai_requirement, self.interval_minutes,
            self.notification_email, self.enabled,
        )


def _search_settings(keywords, mode, min_price, max_price, filter_words) -> tuple:
    return split_keywords(keywords), mode.upper(), min_price, max_price, split_keywords(filter_words)


@dataclass(frozen=True)
class MailBatch:
    message_id: str
    label: str
    recipient: str


@dataclass(frozen=True)
class MailStatus:
    pending: int
    error: str | None
    next_attempt_at: float | None


_MONITOR_COLUMNS = (
    "id, name, keywords, keyword_mode, min_price, max_price, filter_words, "
    "ai_requirement, interval_minutes, notification_email, enabled, "
    "last_scan_at, last_result, created_at, updated_at, rule_version"
)

class Database:
    """A single connection to the SQLite file."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        # Required for ON DELETE CASCADE to actually fire.
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            migrate(self._conn)
        except BaseException:
            self._conn.close()
            raise

    def begin_search(self, monitor: Monitor, initial: dict) -> tuple[str, dict, bool] | None:
        """Load search progress and report whether this call created it."""
        with self._conn:
            created = self._conn.execute(
                """INSERT INTO search_progress
                   SELECT id, ?, ? FROM monitors WHERE id=? AND rule_version=?
                   ON CONFLICT DO NOTHING""",
                (uuid4().hex, json.dumps(initial), monitor.id, monitor.rule_version),
            ).rowcount == 1
            row = self._conn.execute(
                """SELECT generation, state FROM search_progress JOIN monitors ON id=monitor_id
                   WHERE monitor_id=? AND rule_version=?""",
                (monitor.id, monitor.rule_version),
            ).fetchone()
        if row is None:
            return None
        generation, state = row
        return generation, json.loads(state), created

    def rule_is_current(self, monitor: Monitor) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM monitors WHERE id=? AND rule_version=?",
            (monitor.id, monitor.rule_version),
        ).fetchone() is not None

    def search_is_current(self, monitor_id: int, generation: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM search_progress WHERE monitor_id=? AND generation=?",
            (monitor_id, generation),
        ).fetchone() is not None

    def save_search_page(self, monitor_id: int, generation: str, state: dict,
                         candidates: list[tuple], *, rule_version: int, queued_at: float,
                         refresh: Iterable[tuple[str, str, int | None]] = (),
                         discard: Iterable[str] = ()) -> int | None:
        """Return inserted candidates, or None if the search was replaced."""
        with self._conn:
            changed = self._conn.execute(
                """UPDATE search_progress SET state=? WHERE monitor_id=? AND generation=?
                   AND ? = (SELECT rule_version FROM monitors WHERE id=monitor_id)""",
                (json.dumps(state), monitor_id, generation, rule_version),
            ).rowcount
            if not changed:
                return None
            self._conn.executemany(
                """UPDATE scan_candidates
                   SET detail_json=CASE WHEN title IS NOT :title THEN NULL ELSE detail_json END,
                       title=:title, price=:price
                   WHERE monitor_id=:monitor_id AND mercari_id=:mercari_id
                     AND (title IS NOT :title OR price IS NOT :price)""",
                (dict(monitor_id=monitor_id, mercari_id=item_id, title=title, price=price)
                 for item_id, title, price in refresh),
            )
            for item_id in discard:
                self._remove_candidate(monitor_id, item_id)
                self._mark_recheck(monitor_id, item_id)
            return self._conn.executemany(
                """INSERT INTO scan_candidates
                   (monitor_id, mercari_id, title, price, url, item_type, published_at, created_epoch, retry_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                ((monitor_id, *item, queued_at) for item in candidates),
            ).rowcount

    def candidate_rows(self, monitor_id: int, now: float, limit: int | None) -> list[tuple]:
        query = """SELECT mercari_id, title, price, url, item_type, published_at, created_epoch
                   FROM scan_candidates WHERE monitor_id=? AND retry_at<=?"""
        if limit is None:
            return self._conn.execute(f"{query} ORDER BY id", (monitor_id, now)).fetchall()
        if limit <= 0:
            return []
        # Include enough waiting rows to fill the limit even if some also rank as newest.
        rows = self._conn.execute(
            f"{query} ORDER BY retry_at, id LIMIT ?", (monitor_id, now, limit + 1),
        ).fetchall()
        if len(rows) <= limit:
            return rows
        if limit == 1:
            return rows[:1]
        newest = self._conn.execute(
            f"{query} AND failure_count=0 ORDER BY created_epoch DESC NULLS LAST, id LIMIT ?",
            (monitor_id, now, limit // 2),
        ).fetchall()
        selected = {row[0] for row in newest}
        oldest = [row for row in rows if row[0] not in selected][:limit - len(newest)]
        return newest + oldest

    def has_candidate(self, monitor_id: int, mercari_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM scan_candidates WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).fetchone() is not None

    def pending_candidates(self, monitor_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM scan_candidates WHERE monitor_id=?", (monitor_id,)
        ).fetchone()[0]

    def candidate_detail(self, monitor_id: int, mercari_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT detail_json FROM scan_candidates WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).fetchone()
        return row[0] if row is not None else None

    def save_candidate_detail(
        self, monitor: Monitor, generation: str, mercari_id: str, detail_json: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE scan_candidates SET detail_json=?
                   WHERE monitor_id=? AND mercari_id=? AND detail_json IS NULL
                     AND EXISTS (
                         SELECT 1 FROM monitors JOIN search_progress ON monitors.id=search_progress.monitor_id
                         WHERE monitors.id=? AND rule_version=? AND generation=?
                     )""",
                (detail_json, monitor.id, mercari_id, monitor.id, monitor.rule_version, generation),
            )

    def refresh_candidate(self, monitor_id: int, mercari_id: str, title: str,
                          price: int | None, detail_json: str) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE scan_candidates SET title=?, price=?, detail_json=?
                   WHERE monitor_id=? AND mercari_id=?""",
                (title, price, detail_json, monitor_id, mercari_id),
            )

    def discard_candidate(self, monitor_id: int, mercari_id: str) -> None:
        with self._conn:
            self._remove_candidate(monitor_id, mercari_id)
            self._mark_recheck(monitor_id, mercari_id)

    def defer_candidate(self, monitor_id: int, mercari_id: str, failed_at: float,
                        *, rule_version: int) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE scan_candidates
                   SET failure_count=failure_count+1,
                       retry_at=? + CASE failure_count
                           WHEN 0 THEN 300
                           WHEN 1 THEN 900
                           WHEN 2 THEN 3600
                           ELSE 21600
                       END
                   WHERE monitor_id=? AND mercari_id=?
                   AND ? = (SELECT rule_version FROM monitors WHERE id=monitor_id)""",
                (failed_at, monitor_id, mercari_id, rule_version),
            )

    def _remove_candidate(self, monitor_id: int, mercari_id: str) -> None:
        self._conn.execute(
            "DELETE FROM scan_candidates WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        )

    def wants_recheck(self, monitor_id: int, mercari_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM scan_rechecks WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).fetchone() is not None

    def _mark_recheck(self, monitor_id: int, mercari_id: str) -> None:
        self._conn.execute(
            "INSERT INTO scan_rechecks VALUES (?, ?) ON CONFLICT DO NOTHING",
            (monitor_id, mercari_id),
        )

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
        with self._conn:
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
        expected: Monitor | None = None,
    ) -> bool:
        """Update a monitor, returning False if it is missing or its configuration changed."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            previous = self.get_monitor(monitor_id)
            if previous is None:
                return False
            if expected is not None and (
                expected.id != monitor_id or expected.editable_settings() != previous.editable_settings()
            ):
                return False
            search_changed = previous.search_settings() != _search_settings(
                keywords, keyword_mode, min_price, max_price, filter_words,
            )
            rules_changed = search_changed or previous.ai_requirement.strip() != ai_requirement.strip()
            self._conn.execute(
                """
                UPDATE monitors SET name=?, keywords=?, keyword_mode=?,
                    min_price=?, max_price=?, filter_words=?, ai_requirement=?,
                    interval_minutes=?, notification_email=?, enabled=?,
                    updated_at=?, rule_version=rule_version+?
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
                    int(rules_changed),
                    monitor_id,
                ),
            )
            if search_changed:
                self._conn.execute("DELETE FROM search_progress WHERE monitor_id=?", (monitor_id,))
                self._conn.execute("DELETE FROM scan_candidates WHERE monitor_id=?", (monitor_id,))
        return True

    def set_monitor_enabled(self, monitor_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE monitors SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), _now_iso(), monitor_id),
        )
        self._conn.commit()

    def set_last_scan(self, monitor_id: int, result: str, *, rule_version: int | None = None) -> bool:
        updated = self._conn.execute(
            """UPDATE monitors SET last_scan_at=?, last_result=? WHERE id=?
               AND (? IS NULL OR rule_version=?)""",
            (_now_iso(), result, monitor_id, rule_version, rule_version),
        )
        self._conn.commit()
        return updated.rowcount == 1

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
            rule_version=row[15],
        )

    # ------------------------------------------ monitor products (5.2)
    # A row records the monitor's latest completed decision. The history
    # defaults to matches and can also show rejected items.
    def has_monitor_product(self, monitor_id: int, mercari_id: str) -> bool:
        return self.product_decision(monitor_id, mercari_id) is not None

    def product_decision(self, monitor_id: int, mercari_id: str) -> tuple[int, int | None] | None:
        """Return the latest decision as (matched, judged_rule_version)."""
        row = self._conn.execute(
            """SELECT matched, judged_rule_version FROM monitor_products
               WHERE monitor_id=? AND mercari_id=?""",
            (monitor_id, mercari_id),
        ).fetchone()
        return row

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
        mail_batch: MailBatch | None = None,
        rule_version: int | None = None,
    ) -> bool:
        """Record a fully processed item for a monitor.

        matched=True  -> DeepSeek true, or Level 1 pass for a monitor with no AI requirement
        matched=False -> DeepSeek false under the judged rule version.
        Versioned scans may replace a rejection from an older or unknown version.
        published_at is the Mercari listing's own publish time (from the search result's
        `created` field); it stays NULL when unavailable.
        """
        with self._conn:
            inserted = self._conn.execute(
                """
                INSERT INTO monitor_products
                    (monitor_id, mercari_id, title, price, published_at, url,
                     found_at, matched, judged_rule_version)
                SELECT id, ?, ?, ?, ?, ?, ?, ?, rule_version FROM monitors
                WHERE id=? AND (? IS NULL OR rule_version=?)
                  AND NOT EXISTS (
                      SELECT 1 FROM ignored_products WHERE monitor_id=monitors.id AND mercari_id=?
                  )
                ON CONFLICT (monitor_id, mercari_id) DO UPDATE SET
                    title=excluded.title, price=excluded.price, url=excluded.url,
                    published_at=excluded.published_at, found_at=excluded.found_at,
                    matched=excluded.matched, judged_rule_version=excluded.judged_rule_version
                WHERE ? IS NOT NULL AND monitor_products.matched=0
                  AND monitor_products.judged_rule_version IS NOT excluded.judged_rule_version
                """,
                (mercari_id, title, price, published_at, url, _now_iso(), int(matched),
                 monitor_id, rule_version, rule_version, mercari_id, rule_version),
            ).rowcount == 1
            if not inserted:
                return False
            if matched and mail_batch is not None:
                self._conn.execute(
                    """INSERT INTO mail_batches (message_id, monitor_id, label, recipient)
                       VALUES (?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING""",
                    (mail_batch.message_id, monitor_id, mail_batch.label, mail_batch.recipient),
                )
                self._conn.execute(
                    "INSERT INTO mail_items VALUES (?, ?, ?, ?, ?)",
                    (mail_batch.message_id, mercari_id, title, price, url),
                )
            self._remove_candidate(monitor_id, mercari_id)
            self._conn.execute(
                "DELETE FROM scan_rechecks WHERE monitor_id=? AND mercari_id=?",
                (monitor_id, mercari_id),
            )
        return inserted

    def ready_mail(self, now: float, *, message_id: str | None = None) -> None:
        # NULL means the scan is still collecting items. At process startup
        # all remaining NULL rows belong to interrupted scans.
        with self._conn:
            if message_id is None:
                self._conn.execute(
                    "UPDATE mail_batches SET next_attempt_at=? WHERE next_attempt_at IS NULL",
                    (now,),
                )
            else:
                self._conn.execute(
                    """UPDATE mail_batches SET next_attempt_at=?
                       WHERE message_id=? AND next_attempt_at IS NULL""",
                    (now, message_id),
                )

    def next_due_mail(self, now: float) -> tuple[MailBatch, int, list[tuple]] | None:
        row = self._conn.execute(
            """SELECT message_id, label, recipient, attempts FROM mail_batches
               WHERE next_attempt_at <= ? ORDER BY next_attempt_at, rowid LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None
        message_id, label, recipient, attempts = row
        items = self._conn.execute(
            """SELECT mercari_id, title, price, url FROM mail_items
               WHERE message_id=? ORDER BY rowid""",
            (message_id,),
        ).fetchall()
        return MailBatch(message_id, label, recipient), attempts, items

    def retry_mail(self, message_id: str, now: float, delay: int, error: str) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE mail_batches SET attempts=attempts+1, last_error=?,
                   failed_at=?, next_attempt_at=? WHERE message_id=?""",
                (error, now, now + delay, message_id),
            )

    def complete_mail(self, message_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM mail_batches WHERE message_id=?", (message_id,))

    def mail_status(self, monitor_id: int) -> MailStatus:
        pending, = self._conn.execute(
            "SELECT COUNT(*) FROM mail_batches WHERE monitor_id=?", (monitor_id,)
        ).fetchone()
        failure = self._conn.execute(
            """SELECT last_error, next_attempt_at FROM mail_batches
               WHERE monitor_id=? AND failed_at IS NOT NULL
               ORDER BY failed_at DESC, rowid DESC LIMIT 1""",
            (monitor_id,),
        ).fetchone()
        return MailStatus(pending, *(failure or (None, None)))

    def list_monitor_products(
        self,
        monitor_id: int,
        *,
        sort_by: str = "published_desc",
        limit: int = 20,
        offset: int = 0,
        matched: int | None = 1,
        include_decision: bool = False,
    ) -> list[tuple]:
        """History page rows for one monitor.

        (mercari_id, title, price, url, published_at, found_at)
        include_decision appends matched and judged_rule_version to each row.

        matched=1 selects matches (the default), matched=0 selects
        rejections, and matched=None selects both.

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
        decision_columns = ", matched, judged_rule_version" if include_decision else ""
        return self._conn.execute(
            f"SELECT mercari_id, title, price, url, published_at, found_at{decision_columns} "
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
        deleted = self._conn.execute(
            "DELETE FROM monitor_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).rowcount
        if deleted:
            self._mark_recheck(monitor_id, mercari_id)
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
        self._remove_candidate(monitor_id, mercari_id)
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
        deleted = self._conn.execute(
            "DELETE FROM ignored_products WHERE monitor_id=? AND mercari_id=?",
            (monitor_id, mercari_id),
        ).rowcount
        if deleted:
            self._mark_recheck(monitor_id, mercari_id)
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
