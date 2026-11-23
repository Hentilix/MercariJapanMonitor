"""Durable email delivery for the single process that owns the monitor DB."""

import asyncio
import logging
import os
from email.utils import make_msgid
from time import time

from app.database import Database, MailBatch, Monitor
from app.email import ENV_KEYS, send_batch
from app.mercari import SearchItem

log = logging.getLogger(__name__)
RETRY_DELAYS = (60, 300, 900, 3600)
POLL_SECONDS = 15


class MailOutbox:
    def __init__(self, db: Database, *, send=send_batch, clock=time):
        self._db = db
        self._send = send
        self._clock = clock
        self._wake = asyncio.Event()
        self._draining = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._stopping = False

    def new_batch(self, monitor: Monitor) -> MailBatch | None:
        if not all(os.environ.get(key, "").strip() for key in ENV_KEYS):
            return None
        return MailBatch(
            make_msgid(domain="mercari-monitor.local"),
            monitor.name,
            monitor.notification_email or os.environ["SMTP_TO"],
        )

    def finish_batch(self, batch: MailBatch) -> None:
        self._db.ready_mail(self._clock(), message_id=batch.message_id)
        self._wake.set()

    def recover(self) -> None:
        """Call once at startup, before any scans can collect items."""
        self._db.ready_mail(self._clock())

    def start(self) -> None:
        if self._worker is None:
            self.recover()
            self._stopping = False
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._worker is not None:
            await self._worker
            self._worker = None

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                await self.drain_due()
            except Exception:
                log.exception("Could not process the mail queue")
            if not self._stopping:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
                except TimeoutError:
                    pass

    async def drain_due(self) -> None:
        """Send due batches serially. Also used by one-shot scripts."""
        async with self._draining:
            while not self._stopping:
                due = self._db.next_due_mail(self._clock())
                if due is None:
                    return
                batch, attempts, items = due
                try:
                    await self._send(
                        batch.label, [SearchItem(*item) for item in items],
                        recipient=batch.recipient, message_id=batch.message_id,
                    )
                except Exception as exc:
                    delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
                    error = str(exc)[:300] or type(exc).__name__
                    self._db.retry_mail(batch.message_id, self._clock(), delay, error)
                    log.warning("Mail %s failed, retry in %ds: %s", batch.message_id, delay, error)
                else:
                    self._db.complete_mail(batch.message_id)
