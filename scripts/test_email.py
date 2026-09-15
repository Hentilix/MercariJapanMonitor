# -*- coding: utf-8 -*-
"""Send ONE real summary test email (3 sample items) through SMTP.

Phase 5.1 behavior: one batch email per scan task per round — this script
sends exactly one message containing three sample items, over exactly one
SMTP connection.

Usage (from the project root):
    $env:SMTP_HOST = "smtp.qq.com"
    $env:SMTP_PORT = "465"
    $env:SMTP_USERNAME = "hentilix@qq.com"
    $env:SMTP_PASSWORD = "你的QQ邮箱SMTP授权码"
    $env:SMTP_FROM = "hentilix@qq.com"
    $env:SMTP_TO = "chccrimson@gmail.com"
    python scripts/test_email.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.email import send_batch  # noqa: E402
from app.mercari import SearchItem  # noqa: E402

SAMPLE_ITEMS = [
    SearchItem(
        id="mTEST001",
        title="Sample Item One - Built to Spill CD",
        price=1000,
        url="https://jp.mercari.com/item/mTEST001",
    ),
    SearchItem(
        id="mTEST002",
        title="Sample Item Two - Built to Spill CD",
        price=1500,
        url="https://jp.mercari.com/item/mTEST002",
    ),
    SearchItem(
        id="mTEST003",
        title="Sample Item Three - Built to Spill CD",
        price=2000,
        url="https://jp.mercari.com/item/mTEST003",
    ),
]


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        await send_batch("Built to Spill (SMTP batch test)", SAMPLE_ITEMS)
        print("OK: one summary test email (3 items) sent.")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
