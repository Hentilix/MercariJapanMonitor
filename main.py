# -*- coding: utf-8 -*-
"""MercariJapanMonitor — NiceGUI management interface (Phase 6).

Run from the project root:
    python main.py
then open http://127.0.0.1:8081 in the browser.

The port can be overridden with the MJM_PORT environment variable
(default 8081 — 8080 is frequently taken by Steam/other dev tools).
"""

import asyncio
import logging
import logging.handlers
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from nicegui import app, ui

from app.database import Database, Monitor
from app.deepseek import judge_item
from app.email import make_match_notifier
from app.exchange_rate import format_price_pair, format_rate, get_jpy_cny_rate, rate_status
from app.mercari import MercariClient
from app.scanner import scan_monitor as run_monitor_scan
from app.scheduler import MonitorScheduler
from app.single_instance import InstanceLock

log = logging.getLogger("gui")

db: Database
client: MercariClient
ai_http: httpx.AsyncClient
scheduler: MonitorScheduler

MAX_PAGES = 2
PORT = int(os.environ.get("MJM_PORT", "8081"))
LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"

INTERVAL_OPTIONS = {
    5: "5 分钟",
    15: "15 分钟",
    30: "30 分钟",
    60: "1 小时",
    120: "2 小时",
    240: "4 小时",
    480: "8 小时",
    720: "12 小时",
    1440: "24 小时",
}

SMTP_DEFAULTS = {
    "SMTP_HOST": "smtp.qq.com",
    "SMTP_PORT": "465",
    "SMTP_USERNAME": "hentilix@qq.com",
    "SMTP_FROM": "hentilix@qq.com",
    "SMTP_TO": "chccrimson@gmail.com",
}


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                LOG_DIR / "monitor.log",
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        print(f"[MJM] 无法创建日志目录 {LOG_DIR}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _fmt_price(price: int | None) -> str:
    return "no price" if price is None else f"\u00a5{price:,}"


# --------------------------------------------------------------------- scan
async def scan_monitor(monitor_id: int) -> None:
    """One full scan for one monitor (Phase 5.3-A monitor-centric scanner)."""
    mon = db.get_monitor(monitor_id)
    if mon is None:
        return

    judge = None
    if mon.ai_requirement and os.environ.get("DEEPSEEK_API_KEY_FOR_MJM"):

        async def judge_detail(detail):
            return await judge_item(ai_http, mon.ai_requirement, detail)

        judge = judge_detail

    notifier = make_match_notifier(
        label=mon.name, recipient=mon.notification_email
    )

    await run_monitor_scan(
        client,
        db,
        mon,
        judge=judge,
        notifier=notifier,
        max_pages=MAX_PAGES,
    )


# ---------------------------------------------------------------- monitors
@ui.refreshable
def monitors_view() -> None:
    monitors = db.list_monitors()
    if not monitors:
        ui.label("还没有监控任务，点击右上角「新建任务」。").classes("text-gray-500")
        return
    for mon in monitors:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full no-wrap"):
                ui.label(mon.name).classes("text-lg font-bold")
                ui.badge(
                    "启用" if mon.enabled else "已禁用",
                    color="green" if mon.enabled else "grey",
                )
                ui.space()
                ui.button(icon="edit", on_click=lambda m=mon: open_monitor_dialog(m)) \
                    .props("flat dense").tooltip("编辑")
                ui.button(icon="play_arrow", on_click=lambda m=mon: scan_now(m)) \
                    .props("flat dense").tooltip("立即扫描")
                ui.switch(
                    value=mon.enabled,
                    on_change=lambda e, m=mon: toggle_enabled(m, bool(e.value)),
                )
                ui.button(
                    icon="delete", color="negative",
                    on_click=lambda m=mon: ask_delete_monitor(m),
                ).props("flat dense").tooltip("删除")
            with ui.column().classes("gap-0 text-sm text-gray-600"):
                ui.label(f"搜索: {mon.keywords}")
                ui.label(
                    f"匹配: {mon.keyword_mode} | "
                    f"价格: {mon.min_price if mon.min_price is not None else '不限'} ~ "
                    f"{mon.max_price if mon.max_price is not None else '不限'} JPY | "
                    f"排除: {mon.filter_words or '无'} | "
                    f"间隔: {INTERVAL_OPTIONS.get(mon.interval_minutes, f'{mon.interval_minutes} 分钟')}"
                )
                if mon.ai_requirement:
                    ui.label(f"语义要求: {mon.ai_requirement}")
                ui.label(
                    f"通知邮箱: {mon.notification_email or '使用默认邮箱（SMTP_TO）'}"
                )
                ui.label(f"上次扫描: {mon.last_scan_at or '从未'}")
                ui.label(f"上次结果: {mon.last_result or '—'}")


async def toggle_enabled(mon: Monitor, enabled: bool) -> None:
    db.set_monitor_enabled(mon.id, enabled)
    updated = db.get_monitor(mon.id)
    if enabled:
        await scheduler.enable_monitor(updated)  # schedule + immediate scan
    else:
        scheduler.disable_monitor(mon.id)
    monitors_view.refresh()
    ui.notify(f"任务「{mon.name}」已{'启用' if enabled else '禁用'}")


async def scan_now(mon: Monitor) -> None:
    if scheduler.is_running(mon.id):
        ui.notify("该任务正在扫描中，请稍候。", type="warning")
        return
    ui.notify(f"开始扫描「{mon.name}」...", type="info")
    try:
        await scheduler.run_monitor_now(mon.id)
    except Exception:
        log.exception("手动扫描失败: Monitor %s", mon.id)
        ui.notify("扫描失败，请查看日志。", type="negative")
        return
    updated = db.get_monitor(mon.id)
    if updated and updated.last_result:
        ui.notify(
            f"扫描完成: {updated.last_result}",
            type="positive", close_button="关闭", multi_line=True,
        )
    monitors_view.refresh()


def ask_delete_monitor(mon: Monitor) -> None:
    global _delete_pending_id
    _delete_pending_id = mon.id
    delete_message.set_text(
        f"确定删除「{mon.name}」吗？\n\n"
        "删除后将永久删除：\n"
        "- Monitor 配置\n"
        "- 商品历史\n"
        "- 忽略商品记录\n\n"
        "此操作无法撤销。"
    )
    delete_dialog.open()


def confirm_delete_monitor() -> None:
    global _delete_pending_id
    monitor_id = _delete_pending_id
    _delete_pending_id = None
    if monitor_id is not None:
        scheduler.remove_monitor(monitor_id)  # stop scheduling first
        db.delete_monitor(monitor_id)         # cascades products + ignored
    delete_dialog.close()
    monitors_view.refresh()
    ui.notify("已删除任务。")


# ------------------------------------------------------- create/edit dialog
_form_state = {"editing_id": None}

# Dialog elements are created inside the page function (NiceGUI 3.x rule:
# all UI must live in page functions when ui.page is used) and shared with
# the handlers through these globals — single-user local app.
monitor_dialog = None
name_input = query_input = min_input = max_input = None
exclude_input = requirement_input = interval_select = email_input = enabled_switch = None
mode_select = None
delete_dialog = None
delete_message = None
_delete_pending_id = None


# ------------------------------------------- form validation (pure helpers)
def parse_price(value) -> int | None:
    """Convert a form price to int or None. Raises ValueError when invalid.

    "" / None -> None (no limit). Fractions and negatives are rejected.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            raise ValueError("价格必须是整数。") from None
    else:
        number = float(value)
    if not number.is_integer():
        raise ValueError("价格必须是整数。")
    if number < 0:
        raise ValueError("价格不能为负数。")
    return int(number)


def validate_monitor_form(
    name: str,
    keywords: str,
    min_price,
    max_price,
    keyword_mode: str,
    ai_requirement: str,
) -> str | None:
    """Return a user-facing error message, or None when the form is valid."""
    if not (name or "").strip():
        return "任务名称不能为空。"
    if not (keywords or "").strip():
        return "搜索关键词不能为空。"
    if keyword_mode not in ("AND", "OR"):
        return "匹配模式必须是 AND 或 OR。"
    if not (ai_requirement or "").strip():
        return "AI 要求不能为空。"
    try:
        min_p = parse_price(min_price)
        max_p = parse_price(max_price)
    except ValueError as exc:
        return str(exc)
    if min_p is not None and max_p is not None and min_p > max_p:
        return "最低价格不能高于最高价格。"
    return None


def monitor_fields_from_form(
    name: str,
    keywords: str,
    min_price,
    max_price,
    keyword_mode: str,
    filter_words: str,
    ai_requirement: str,
    interval_minutes: int,
    notification_email: str,
    enabled: bool,
) -> dict:
    """Build the create/update kwargs from raw form values.

    Blank prices/email become None (None = no limit / use SMTP_TO).
    Keywords/filter words are stored as the raw user string — the scanner
    splits them with split_keywords() later.
    """
    return dict(
        name=name.strip(),
        keywords=keywords.strip(),
        keyword_mode=keyword_mode,
        min_price=parse_price(min_price),
        max_price=parse_price(max_price),
        filter_words=(filter_words or "").strip(),
        ai_requirement=(ai_requirement or "").strip(),
        interval_minutes=int(interval_minutes),
        notification_email=(notification_email or "").strip() or None,
        enabled=bool(enabled),
    )


def open_monitor_dialog(mon: Monitor | None = None) -> None:
    _form_state["editing_id"] = mon.id if mon else None
    name_input.value = mon.name if mon else ""
    query_input.value = mon.keywords if mon else ""
    min_input.value = mon.min_price if mon else None
    max_input.value = mon.max_price if mon else None
    exclude_input.value = mon.filter_words if mon else ""
    mode_select.value = mon.keyword_mode if mon else "AND"
    requirement_input.value = (mon.ai_requirement or "") if mon else ""
    interval_select.value = mon.interval_minutes if mon else 30
    email_input.value = (mon.notification_email or "") if mon else ""
    enabled_switch.value = mon.enabled if mon else True
    monitor_dialog.open()


async def save_monitor() -> None:
    error = validate_monitor_form(
        name_input.value or "",
        query_input.value or "",
        min_input.value,
        max_input.value,
        mode_select.value or "",
        requirement_input.value or "",
    )
    if error:
        ui.notify(f"保存失败：{error}", type="negative")
        return
    fields = monitor_fields_from_form(
        name_input.value or "",
        query_input.value or "",
        min_input.value,
        max_input.value,
        mode_select.value or "AND",
        exclude_input.value or "",
        requirement_input.value or "",
        int(interval_select.value or 30),
        email_input.value or "",
        bool(enabled_switch.value),
    )
    editing_id = _form_state["editing_id"]
    try:
        if editing_id is None:
            new_id = db.create_monitor(**fields)
            created = db.get_monitor(new_id)
            if created and created.enabled:
                await scheduler.enable_monitor(created)  # schedule + immediate scan
            ui.notify(f"已创建任务「{fields['name']}」")
        else:
            db.update_monitor(editing_id, **fields)
            updated = db.get_monitor(editing_id)
            if updated and updated.enabled:
                await scheduler.enable_monitor(updated)  # reschedule + scan now
            else:
                scheduler.disable_monitor(editing_id)
            ui.notify(f"已保存任务「{fields['name']}」")
    except sqlite3.IntegrityError:
        log.exception("保存 Monitor 失败（名称冲突）")
        ui.notify("保存失败：已经存在同名任务，请换一个名称。", type="negative")
        return
    monitor_dialog.close()
    monitors_view.refresh()


# --------------------------------------------------- 商品历史 (Phase 5.4-B)
HISTORY_SORT_OPTIONS = {
    "published_desc": "发布时间：新 → 旧",
    "published_asc": "发布时间：旧 → 新",
    "price_desc": "价格：高 → 低",
    "price_asc": "价格：低 → 高",
}

HISTORY_PAGE_SIZE = 20

_history_state = {"monitor_id": None, "page": 1, "sort_by": "published_desc"}
_ignored_state = {"page": 1}
_product_delete_state = {"monitor_id": None, "mercari_id": None}
_ignore_state = {"monitor_id": None, "mercari_id": None}
_unignore_state = {"monitor_id": None, "mercari_id": None}

product_delete_dialog = None
product_delete_message = None
ignore_dialog = None
ignore_message = None
unignore_dialog = None
unignore_message = None


def calculate_total_pages(total: int, page_size: int) -> int:
    """Ceil(total / page_size), at least 1 page."""
    if page_size <= 0:
        page_size = 1
    return max(1, (total + page_size - 1) // page_size)


def clamp_page(page: int, total_pages: int) -> int:
    return max(1, min(page, total_pages))


def _rate_banner_text(rate: float | None) -> str:
    banner = format_rate(rate)
    if rate_status() == "fallback":
        banner += "（使用上次成功汇率）"
    return banner


def _current_history_monitor(monitors) -> Monitor | None:
    state = _history_state
    current = state["monitor_id"]
    valid_ids = {m.id for m in monitors}
    if current not in valid_ids:  # never selected, or the monitor was deleted
        current = monitors[0].id if monitors else None
        state["monitor_id"] = current
        state["page"] = 1
    if current is None:
        return None
    return next(m for m in monitors if m.id == current)


def _on_history_monitor_changed(monitor_id: int) -> None:
    _history_state["monitor_id"] = monitor_id
    _history_state["page"] = 1  # switching monitors always restarts at page 1
    _ignored_state["page"] = 1
    history_view.refresh()
    ignored_view.refresh()


def _on_ignored_monitor_changed(monitor_id: int) -> None:
    _on_history_monitor_changed(monitor_id)  # shared monitor selection


def _on_history_sort_changed(sort_by: str) -> None:
    _history_state["sort_by"] = sort_by
    _history_state["page"] = 1  # a new ordering restarts at page 1
    history_view.refresh()


def _change_history_page(delta: int) -> None:
    _history_state["page"] += delta
    history_view.refresh()


def ask_delete_product(monitor_id: int, mercari_id: str) -> None:
    _product_delete_state["monitor_id"] = monitor_id
    _product_delete_state["mercari_id"] = mercari_id
    product_delete_message.set_text(
        "确定从当前 Monitor 中删除这个商品吗？\n\n"
        "删除后，如果以后再次搜索到该商品，它仍然可以被重新处理。"
    )
    product_delete_dialog.open()


def confirm_delete_product() -> None:
    monitor_id = _product_delete_state["monitor_id"]
    mercari_id = _product_delete_state["mercari_id"]
    _product_delete_state["monitor_id"] = None
    _product_delete_state["mercari_id"] = None
    if monitor_id is not None and mercari_id is not None:
        # Per (monitor_id, mercari_id) — other monitors are untouched.
        db.delete_monitor_product(monitor_id, mercari_id)
    product_delete_dialog.close()
    history_view.refresh()
    ui.notify("已从历史中删除（未来可重新处理）。")


# ------------------------------------------------------- ignore (Phase 5.4-C)
def ask_ignore_product(monitor_id: int, mercari_id: str) -> None:
    _ignore_state["monitor_id"] = monitor_id
    _ignore_state["mercari_id"] = mercari_id
    ignore_message.set_text(
        "确定要忽略这个商品吗？\n\n"
        "忽略后：\n"
        "• 商品会从当前 Monitor 的历史中移除\n"
        "• 以后扫描到这个商品时会永久跳过\n"
        "• 只有取消忽略后，未来才可以重新处理"
    )
    ignore_dialog.open()


def confirm_ignore_product() -> None:
    monitor_id = _ignore_state["monitor_id"]
    mercari_id = _ignore_state["mercari_id"]
    _ignore_state["monitor_id"] = None
    _ignore_state["mercari_id"] = None
    if monitor_id is not None and mercari_id is not None:
        try:
            db.ignore_product(monitor_id, mercari_id)  # removes history + adds ignored
        except Exception:
            log.exception("忽略商品失败: monitor=%s mercari=%s", monitor_id, mercari_id)
            ui.notify("忽略失败，请查看日志。", type="negative")
            return
    ignore_dialog.close()
    history_view.refresh()
    ignored_view.refresh()
    ui.notify("已忽略该商品（永久跳过）。")


def ask_unignore_product(monitor_id: int, mercari_id: str) -> None:
    _unignore_state["monitor_id"] = monitor_id
    _unignore_state["mercari_id"] = mercari_id
    unignore_message.set_text(
        "确定取消忽略这个商品吗？\n\n"
        "取消后：\n以后扫描到这个商品时，它可以重新经过筛选。"
    )
    unignore_dialog.open()


def confirm_unignore_product() -> None:
    monitor_id = _unignore_state["monitor_id"]
    mercari_id = _unignore_state["mercari_id"]
    _unignore_state["monitor_id"] = None
    _unignore_state["mercari_id"] = None
    if monitor_id is not None and mercari_id is not None:
        try:
            db.unignore_product(monitor_id, mercari_id)  # only removes ignored
        except Exception:
            log.exception("取消忽略失败: monitor=%s mercari=%s", monitor_id, mercari_id)
            ui.notify("取消忽略失败，请查看日志。", type="negative")
            return
    unignore_dialog.close()
    ignored_view.refresh()  # NOT re-added to history — next scan decides
    ui.notify("已取消忽略（下次扫描可重新处理）。")


@ui.refreshable
async def history_view() -> None:
    state = _history_state
    monitors = db.list_monitors()
    if not monitors:
        ui.label("暂无 Monitor，请先创建一个任务。").classes("text-gray-500")
        return
    monitor = _current_history_monitor(monitors)

    rate = await get_jpy_cny_rate()  # at most one request per day

    with ui.row().classes("items-center w-full"):
        ui.select(
            {str(m.id): m.name for m in monitors},
            value=str(monitor.id),  # option keys are strings
            label="Monitor",
            on_change=lambda e: _on_history_monitor_changed(int(e.value)),
        ).props("dense")
        ui.space()
        ui.select(
            HISTORY_SORT_OPTIONS,
            value=state["sort_by"],
            label="排序",
            on_change=lambda e: _on_history_sort_changed(str(e.value)),
        ).props("dense")
    ui.label(_rate_banner_text(rate)).classes("text-xs text-gray-500")

    total = db.count_monitor_products(monitor.id)
    total_pages = calculate_total_pages(total, HISTORY_PAGE_SIZE)
    state["page"] = clamp_page(state["page"], total_pages)  # delete 边界自动回退
    offset = (state["page"] - 1) * HISTORY_PAGE_SIZE
    rows = db.list_monitor_products(
        monitor.id,
        sort_by=state["sort_by"],
        limit=HISTORY_PAGE_SIZE,
        offset=offset,
    )

    if not rows:
        ui.label("暂无匹配商品").classes("text-gray-500")
    else:
        for mercari_id, title, price, url, published_at, _found_at in rows:
            with ui.row().classes("items-center w-full no-wrap"):
                ui.link(title, target=url, new_tab=True)
                jpy_text, cny_text = format_price_pair(price, rate)
                with ui.column().classes("gap-0"):
                    ui.label(jpy_text).classes("text-sm font-medium")
                    ui.label(cny_text).classes("text-xs text-gray-500")
                ui.label(
                    f"发布时间: {published_at or '未知'}"
                ).classes("text-sm text-gray-500")
                ui.space()
                ui.button(
                    icon="delete",
                    color="negative",
                    on_click=lambda m=mercari_id: ask_delete_product(monitor.id, m),
                ).props("flat dense").tooltip("从历史中删除（未来可重新处理）")
                ui.button(
                    icon="block",
                    color="warning",
                    on_click=lambda m=mercari_id: ask_ignore_product(monitor.id, m),
                ).props("flat dense").tooltip("忽略（移除历史并永久跳过）")

    with ui.row().classes("items-center justify-center w-full gap-2"):
        prev_button = ui.button("← 上一页", on_click=lambda: _change_history_page(-1)) \
            .props("flat dense")
        ui.label(f"第 {state['page']} / {total_pages} 页") \
            .classes("text-sm text-gray-600")
        next_button = ui.button("下一页 →", on_click=lambda: _change_history_page(1)) \
            .props("flat dense")
    prev_button.set_enabled(state["page"] > 1)
    next_button.set_enabled(state["page"] < total_pages)


# ------------------------------------------------------ 忽略列表 (Phase 5.4-C)
def _change_ignored_page(delta: int) -> None:
    _ignored_state["page"] += delta
    ignored_view.refresh()


@ui.refreshable
async def ignored_view() -> None:
    monitors = db.list_monitors()
    if not monitors:
        ui.label("暂无 Monitor，请先创建一个任务。").classes("text-gray-500")
        return
    monitor = _current_history_monitor(monitors)  # shared selection with history

    rate = await get_jpy_cny_rate()  # cached: at most one request per day

    with ui.row().classes("items-center w-full"):
        ui.select(
            {str(m.id): m.name for m in monitors},
            value=str(monitor.id),  # option keys are strings
            label="Monitor",
            on_change=lambda e: _on_ignored_monitor_changed(int(e.value)),
        ).props("dense")
        ui.space()
        ui.label("忽略列表（最新忽略在前）").classes("text-sm text-gray-500")
    ui.label(_rate_banner_text(rate)).classes("text-xs text-gray-500")

    total = db.count_ignored_products(monitor.id)
    total_pages = calculate_total_pages(total, HISTORY_PAGE_SIZE)
    _ignored_state["page"] = clamp_page(_ignored_state["page"], total_pages)
    offset = (_ignored_state["page"] - 1) * HISTORY_PAGE_SIZE
    rows = db.list_ignored_products(
        monitor.id, limit=HISTORY_PAGE_SIZE, offset=offset
    )

    if not rows:
        ui.label("暂无忽略商品").classes("text-gray-500")
    else:
        for mercari_id, title, price, url, ignored_at in rows:
            with ui.row().classes("items-center w-full no-wrap"):
                if title and url:
                    ui.link(title, target=url, new_tab=True)
                else:
                    ui.label(title or "未知商品").classes("font-medium")
                jpy_text, cny_text = format_price_pair(price, rate)
                with ui.column().classes("gap-0"):
                    ui.label(jpy_text).classes("text-sm font-medium")
                    ui.label(cny_text).classes("text-xs text-gray-500")
                ui.label(f"忽略时间: {ignored_at or '未知'}") \
                    .classes("text-sm text-gray-500")
                ui.space()
                ui.button(
                    icon="undo",
                    color="primary",
                    on_click=lambda m=mercari_id: ask_unignore_product(monitor.id, m),
                ).props("flat dense").tooltip("取消忽略（下次扫描可重新处理）")

    with ui.row().classes("items-center justify-center w-full gap-2"):
        prev_button = ui.button("← 上一页", on_click=lambda: _change_ignored_page(-1)) \
            .props("flat dense")
        ui.label(f"第 {_ignored_state['page']} / {total_pages} 页") \
            .classes("text-sm text-gray-600")
        next_button = ui.button("下一页 →", on_click=lambda: _change_ignored_page(1)) \
            .props("flat dense")
    prev_button.set_enabled(_ignored_state["page"] > 1)
    next_button.set_enabled(_ignored_state["page"] < total_pages)


# ---------------------------------------------------------------- settings
def save_settings(
    key_input, host_input, port_input, user_input, pass_input,
    from_input, to_input,
) -> None:
    pairs = [
        ("DEEPSEEK_API_KEY_FOR_MJM", key_input.value),
        ("SMTP_HOST", host_input.value),
        ("SMTP_PORT", port_input.value),
        ("SMTP_USERNAME", user_input.value),
        ("SMTP_PASSWORD", pass_input.value),
        ("SMTP_FROM", from_input.value),
        ("SMTP_TO", to_input.value),
    ]
    changed = []
    for env_name, value in pairs:
        if value and value.strip():
            os.environ[env_name] = value.strip()
            changed.append(env_name)
    # Clear secrets from the UI immediately.
    for inp in (key_input, pass_input):
        inp.value = ""
    if changed:
        ui.notify(f"已更新环境变量: {', '.join(changed)}")
    else:
        ui.notify("没有输入任何值", type="warning")
    settings_status.refresh()


@ui.refreshable
def settings_status() -> None:
    deepseek = "已设置" if os.environ.get("DEEPSEEK_API_KEY_FOR_MJM") else "未设置"
    smtp = (
        "已配置" if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_PASSWORD")
        else "未配置"
    )
    ui.label(f"DeepSeek Key: {deepseek} | SMTP: {smtp}").classes("text-sm text-gray-600")


# --------------------------------------------------------------------- page
@ui.page("/")
def index() -> None:
    # NiceGUI 3.x requires ALL UI inside page functions when ui.page is used,
    # so the dialog is built here and its elements are shared via globals
    # (single-user local app).
    global monitor_dialog, name_input, query_input, min_input, max_input
    global exclude_input, requirement_input, interval_select, email_input
    global enabled_switch, mode_select, delete_dialog, delete_message
    global _delete_pending_id, product_delete_dialog, product_delete_message
    global ignore_dialog, ignore_message, unignore_dialog, unignore_message

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        ui.label("Mercari Japan Monitor").classes("text-3xl font-bold")
        ui.label("Scheduler: Running | 已启用任务按各自间隔自动扫描") \
            .classes("text-gray-500")

        with ui.expansion("设置 (DeepSeek / SMTP)", icon="settings").classes("w-full"):
            with ui.column().classes("gap-2"):
                settings_status()
                ui.label(
                    "写入当前进程的环境变量，不落盘；留空 = 保持不变。"
                    "重启后请通过环境变量重新设置。"
                ).classes("text-xs text-gray-500")
                key_input = ui.input(
                    "DeepSeek API Key (DEEPSEEK_API_KEY_FOR_MJM)",
                    password=True, password_toggle_button=True,
                ).props("dense")
                host_input = ui.input(
                    "SMTP_HOST", placeholder=SMTP_DEFAULTS["SMTP_HOST"]
                ).props("dense")
                port_input = ui.input(
                    "SMTP_PORT", placeholder=SMTP_DEFAULTS["SMTP_PORT"]
                ).props("dense")
                user_input = ui.input(
                    "SMTP_USERNAME", placeholder=SMTP_DEFAULTS["SMTP_USERNAME"]
                ).props("dense")
                pass_input = ui.input(
                    "SMTP_PASSWORD (QQ 授权码)", password=True,
                    password_toggle_button=True,
                ).props("dense")
                from_input = ui.input(
                    "SMTP_FROM", placeholder=SMTP_DEFAULTS["SMTP_FROM"]
                ).props("dense")
                to_input = ui.input(
                    "SMTP_TO (默认收件人)", placeholder=SMTP_DEFAULTS["SMTP_TO"]
                ).props("dense")
                ui.button(
                    "保存设置",
                    on_click=lambda: save_settings(
                        key_input, host_input, port_input, user_input,
                        pass_input, from_input, to_input,
                    ),
                )

        with ui.row().classes("w-full items-center"):
            ui.label("监控任务").classes("text-xl font-bold")
            ui.space()
            ui.button("新建任务", icon="add", on_click=lambda: open_monitor_dialog(None))

        monitors_view()

        ui.separator()
        ui.label("匹配商品历史（按 Monitor 查看）").classes("text-xl font-bold")
        history_view()

        ui.separator()
        ui.label("忽略商品（按 Monitor 查看）").classes("text-xl font-bold")
        ignored_view()

    # monitor form dialog
    with ui.dialog() as monitor_dialog, ui.card().classes("w-[560px]"):
        ui.label("监控任务").classes("text-lg font-bold")
        name_input = ui.input("任务名称（必填，必须唯一）").props("dense")
        query_input = ui.input(
            "搜索关键词（必填，多个关键词请用中文逗号，分割）",
            placeholder="John Coltrane，A Love Supreme，CD",
        ).props("dense")
        mode_select = ui.select(
            {
                "AND": "AND（全部关键词都要命中）",
                "OR": "OR（任一关键词命中即可）",
            },
            label="关键词匹配模式",
            value="AND",
        ).props("dense")
        with ui.row().classes("w-full"):
            min_input = ui.number(
                "最低价格 JPY（可选）", value=None, min=0, precision=0
            ).props("dense").classes("w-1/2")
            max_input = ui.number(
                "最高价格 JPY（可选）", value=None, min=0, precision=0
            ).props("dense").classes("w-1/2")
        exclude_input = ui.input(
            "排除关键词（中文逗号，分割，任一命中即排除）",
            placeholder="LP，DVD，Blu-ray",
        ).props("dense")
        requirement_input = ui.textarea(
            "AI 语义要求（必填，交给 DeepSeek 判断，不限长度）",
            placeholder="只保留 CD 格式的正式发行，不要黑胶、DVD、盗版或数字版本。",
        ).props("dense")
        interval_select = ui.select(
            INTERVAL_OPTIONS, label="扫描间隔", value=30
        ).props("dense")
        email_input = ui.input(
            "通知邮箱（可选，留空 = 使用默认收件人 SMTP_TO）",
            placeholder="留空则使用默认收件人",
        ).props("dense")
        enabled_switch = ui.switch("启用（保存后立即扫描一次）", value=True)
        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=monitor_dialog.close).props("flat")
            ui.button("保存", on_click=save_monitor)

    # delete confirmation dialog
    with ui.dialog() as delete_dialog, ui.card():
        ui.label("删除任务").classes("text-lg font-bold")
        delete_message = ui.label("").classes("whitespace-pre-line")
        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=delete_dialog.close).props("flat")
            ui.button(
                "确认删除", color="negative", on_click=confirm_delete_monitor
            )

    # history item delete confirmation dialog
    with ui.dialog() as product_delete_dialog, ui.card():
        ui.label("删除商品").classes("text-lg font-bold")
        product_delete_message = ui.label("").classes("whitespace-pre-line")
        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=product_delete_dialog.close).props("flat")
            ui.button(
                "确认删除", color="negative", on_click=confirm_delete_product
            )

    # ignore confirmation dialog
    with ui.dialog() as ignore_dialog, ui.card():
        ui.label("忽略商品").classes("text-lg font-bold")
        ignore_message = ui.label("").classes("whitespace-pre-line")
        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=ignore_dialog.close).props("flat")
            ui.button(
                "确认忽略", color="warning", on_click=confirm_ignore_product
            )

    # unignore confirmation dialog
    with ui.dialog() as unignore_dialog, ui.card():
        ui.label("取消忽略").classes("text-lg font-bold")
        unignore_message = ui.label("").classes("whitespace-pre-line")
        with ui.row().classes("justify-end w-full"):
            ui.button("取消", on_click=unignore_dialog.close).props("flat")
            ui.button(
                "确认取消忽略", color="primary", on_click=confirm_unignore_product
            )


# --------------------------------------------------------------------- main
def main() -> int:
    setup_logging()
    instance_lock = InstanceLock()
    if not instance_lock.acquire():
        log.warning(
            "另一个 MarketplaceMonitor 实例正在运行（锁文件 %s），本次启动直接退出。",
            instance_lock.path,
        )
        print(
            "[MJM] 检测到已有 MarketplaceMonitor 实例在运行，本次启动退出。",
            file=sys.stderr,
        )
        return 1
    global db, client, ai_http, scheduler
    db = Database()
    client = MercariClient()
    ai_http = httpx.AsyncClient(timeout=30.0)
    scheduler = MonitorScheduler(scan_monitor)

    @app.on_startup
    async def startup() -> None:
        monitors = db.list_monitors()
        for mon in monitors:
            if mon.enabled:
                log.info("Monitor %s (%s) 已启用", mon.id, mon.name)
            else:
                log.info("Monitor %s (%s) 已禁用（不扫描、不调度）", mon.id, mon.name)
        # Registers one job per enabled monitor and immediately scans each
        # enabled monitor once (fire-and-forget, independent per monitor).
        scheduler.start(monitors)
        enabled_count = sum(1 for m in monitors if m.enabled)
        log.info(
            "Scheduler started: %d monitor(s), %d enabled. GUI at http://127.0.0.1:%d",
            len(monitors),
            enabled_count,
            PORT,
        )

    @app.on_shutdown
    async def shutdown() -> None:
        log.info("Shutting down...")
        await scheduler.stop()
        db.close()
        await client.close()
        await ai_http.aclose()
        log.info("Stopped. Bye.")

    try:
        ui.run(
            title="MercariJapanMonitor",
            host="127.0.0.1",
            port=PORT,
            reload=False,
            show=False,
            language="zh-CN",
        )
    finally:
        instance_lock.release()
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
