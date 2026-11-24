"""SMTP email notification. Standard library only.

QQ Mail specifics (verified against current QQ Mail docs):
    host: smtp.qq.com
    port: 465 with implicit SSL (smtplib.SMTP_SSL)
    password: the SMTP authorization code (授权码) — NOT the QQ login
    password. It must come from the SMTP_PASSWORD environment variable.

Notification model (Phase 5.1): ONE summary email per scan task per round.
send_batch() opens exactly ONE SMTP connection (connect -> login ->
send_message -> quit) and puts all matched items into a single message —
QQ SMTP limits short bursts of repeated connections, so we never send
one email per item.

smtplib is synchronous, so the actual send runs in a worker thread
(asyncio.to_thread) to keep the event loop and the APScheduler job free.
"""

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from contextlib import suppress

from app.mercari import SearchItem

log = logging.getLogger(__name__)

ENV_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
)

SEPARATOR = "=" * 50
ITEM_SEPARATOR = "-" * 50


def _read_config() -> dict | None:
    """Read SMTP settings from environment variables.

    Returns None (with a warning) when any variable is missing. The
    password is read but never logged.
    """
    missing = [key for key in ENV_KEYS if not os.environ.get(key)]
    if missing:
        log.warning("SMTP not configured — missing: %s", ", ".join(missing))
        return None
    return {
        "host": os.environ["SMTP_HOST"],
        "port": int(os.environ["SMTP_PORT"]),
        "username": os.environ["SMTP_USERNAME"],
        "password": os.environ["SMTP_PASSWORD"],
        "sender": os.environ["SMTP_FROM"],
        "recipient": os.environ["SMTP_TO"],
    }


def build_message(sender: str, recipient: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def build_batch_subject(label: str, count: int) -> str:
    """Subject uses the MONITOR NAME (Phase 5.3-A), not the raw query."""
    return f"Mercari 新商品提醒｜{label}｜{count} 件"


def build_batch_body(label: str, items: list[SearchItem]) -> str:
    lines = [
        "Mercari 新商品提醒",
        "",
        f"任务：{label}",
        "",
        f"本次发现 {len(items)} 件符合条件的新商品：",
        "",
        SEPARATOR,
        "",
    ]
    for index, item in enumerate(items, start=1):
        price = "no price" if item.price is None else f"\u00a5{item.price:,}"
        lines.append(f"{index}. {item.title}")
        lines.append(f"价格：{price}")
        lines.append(f"链接：{item.url}")
        if index < len(items):
            lines.append("")
            lines.append(ITEM_SEPARATOR)
        lines.append("")
    lines.append(SEPARATOR)
    lines.append("")
    lines.append("以上商品符合此任务的筛选条件。")
    return "\n".join(lines)


def _send_sync(msg: EmailMessage, config: dict) -> None:
    server = smtplib.SMTP_SSL(
        config["host"], config["port"], timeout=30, context=ssl.create_default_context(),
    )
    try:
        server.login(config["username"], config["password"])
        refused = server.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # QUIT cannot undo an accepted DATA command.
        with suppress(smtplib.SMTPException, OSError):
            server.quit()
        with suppress(OSError):
            server.close()


async def send_batch(
    label: str, items: list[SearchItem], recipient: str | None = None,
    *, message_id: str | None = None,
) -> None:
    """Send ONE summary email containing all matched items.

    Exactly one SMTP connection per call: connect -> login -> send -> quit.
    An empty item list sends nothing. Raises RuntimeError on any failure.

    `label` is the monitor name (used in the subject/body).
    `recipient` overrides the SMTP_TO environment variable (per-monitor
    notification addresses).
    """
    if not items:
        return
    config = _read_config()
    if config is None:
        raise RuntimeError("SMTP environment variables are not set")
    if recipient:
        config["recipient"] = recipient
    msg = build_message(
        config["sender"],
        config["recipient"],
        build_batch_subject(label, len(items)),
        build_batch_body(label, items),
    )
    if message_id is not None:
        msg["Message-ID"] = message_id
    try:
        await asyncio.to_thread(_send_sync, msg, config)
    except (smtplib.SMTPException, OSError) as exc:
        raise RuntimeError(f"SMTP send failed: {type(exc).__name__}: {exc}") from exc
    log.info(
        "Batch email sent to %s: %d item(s) for %s",
        config["recipient"],
        len(items),
        label,
    )
