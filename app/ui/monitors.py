import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

from nicegui import ui

from app.database import Database, Monitor, split_keywords
from app.scheduler import MonitorScheduler, ScanStatus

from .confirmation import Confirmation

log = logging.getLogger("gui")

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
) -> str | None:
    """Return a user-facing error message, or None when the form is valid."""
    if not (name or "").strip():
        return "任务名称不能为空。"
    if not split_keywords(keywords):
        return "请至少填写一个搜索关键词。"
    if keyword_mode not in ("AND", "OR"):
        return "匹配模式必须是 AND 或 OR。"
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


class MonitorsView:
    def __init__(
        self, db: Database, scheduler: MonitorScheduler, on_tasks_changed: Callable[[], None],
    ) -> None:
        self.db = db
        self.scheduler = scheduler
        self.on_tasks_changed = on_tasks_changed
        self.editing_monitor: Monitor | None = None
        self.delete_confirmation = Confirmation("删除任务", "确认删除", "negative")
        self._build_form()

    def build(self) -> None:
        with ui.row().classes("w-full items-center"):
            ui.label("监控任务").classes("text-xl font-bold")
            ui.space()
            ui.button("新建任务", icon="add", on_click=lambda: self.open_form())
        self.panel = ui.column().classes("w-full")
        self.refresh()

    def refresh(self) -> None:
        self.panel.clear()
        with self.panel:
            self._render()

    def mail_status_text(self, monitor_id: int) -> str:
        status = self.db.mail_status(monitor_id)
        text = f"待发邮件 {status.pending} 封"
        if status.error and status.next_attempt_at is not None:
            retry_at = datetime.fromtimestamp(status.next_attempt_at).strftime("%Y-%m-%d %H:%M:%S")
            text += f" · 最近失败：{status.error} · 下次重试：{retry_at}"
        return text


    def scan_status_text(self, monitor_id: int) -> str:
        monitor = self.db.get_monitor(monitor_id)
        if monitor is None:
            return ""
        return f"上次扫描 {monitor.last_scan_at or '从未'} · {monitor.last_result or '暂无结果'}"

    def _render(self) -> None:
        monitors = self.db.list_monitors()
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
                    ui.button(icon="edit", on_click=lambda m=mon: self.open_form(m.id)) \
                        .props("flat dense").tooltip("编辑")
                    ui.button(icon="play_arrow", on_click=lambda m=mon: self.scan_now(m.id)) \
                        .props("flat dense").tooltip("立即扫描")
                    ui.switch(
                        value=mon.enabled,
                        on_change=lambda e, m=mon: self.toggle_enabled(m.id, bool(e.value)),
                    )
                    ui.button(
                        icon="delete", color="negative",
                        on_click=lambda m=mon: self.ask_delete(m),
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
                    scan_label = ui.label(self.scan_status_text(mon.id)).classes("break-all")
                    ui.timer(5, lambda label=scan_label, mid=mon.id: label.set_text(self.scan_status_text(mid)))
                    status_label = ui.label(self.mail_status_text(mon.id)).classes("break-all")
                    ui.timer(5, lambda label=status_label, mid=mon.id: label.set_text(self.mail_status_text(mid)))

    def _apply_schedule(self, monitor: Monitor) -> None:
        self.scheduler.schedule(monitor)
        if monitor.enabled:
            self.scheduler.request_scan(monitor.id)

    def toggle_enabled(self, monitor_id: int, enabled: bool) -> None:
        self.db.set_monitor_enabled(monitor_id, enabled)
        updated = self.db.get_monitor(monitor_id)
        if updated is not None:
            self._apply_schedule(updated)
            ui.notify(f"任务「{updated.name}」已{'启用' if enabled else '禁用'}")
        else:
            ui.notify("任务已被删除。", type="warning")
        self.refresh()

    async def scan_now(self, monitor_id: int) -> None:
        client = ui.context.client
        if self.db.get_monitor(monitor_id) is None:
            ui.notify("任务已被删除。", type="warning")
            self.refresh()
            return
        if self.scheduler.is_running(monitor_id):
            ui.notify("该任务正在扫描中，请稍候。", type="warning")
            return
        ui.notify("开始扫描…", type="info")
        try:
            outcome = await self.scheduler.run_monitor_now(monitor_id)
        except Exception:
            log.exception("手动扫描失败: Monitor %s", monitor_id)
            if not client.is_deleted:
                with client:
                    ui.notify("扫描失败，请查看日志。", type="negative")
            return
        if client.is_deleted:
            return
        with client:
            if outcome.status is ScanStatus.ALREADY_RUNNING:
                ui.notify("该任务正在扫描中，请稍候。", type="warning")
            elif outcome.status is ScanStatus.STOPPING:
                ui.notify("程序正在停止，无法开始扫描。", type="warning")
            elif outcome.status is ScanStatus.FAILED:
                ui.notify("扫描失败，请查看日志。", type="negative")
            elif outcome.status is ScanStatus.INVALIDATED:
                ui.notify("任务已删除或扫描配置已变更，本次结果已失效。", type="warning")
            elif outcome.status is ScanStatus.COMPLETED:
                ui.notify(
                    f"扫描完成: {outcome.summary}",
                    type="positive", close_button="关闭", multi_line=True,
                )
            self.refresh()

    def ask_delete(self, monitor: Monitor) -> None:
        self.delete_confirmation.open(
            f"确定删除「{monitor.name}」吗？\n\n"
            "任务配置、商品历史和忽略记录将永久删除。",
            lambda: self._delete(monitor.id),
        )

    def _delete(self, monitor_id: int) -> None:
        if self.db.get_monitor(monitor_id) is None:
            ui.notify("任务已被删除。", type="warning")
        else:
            self.scheduler.remove_monitor(monitor_id)
            self.db.delete_monitor(monitor_id)
            ui.notify("已删除任务。")
        self.refresh()
        self.on_tasks_changed()

    def open_form(self, monitor_id: int | None = None) -> None:
        monitor = self.db.get_monitor(monitor_id) if monitor_id is not None else None
        if monitor_id is not None and monitor is None:
            ui.notify("任务已被删除。", type="warning")
            self.refresh()
            return
        self._show_form(monitor)

    def reload_latest(self) -> None:
        if self.editing_monitor is None:
            return
        monitor = self.db.get_monitor(self.editing_monitor.id)
        if monitor is None:
            self._missing_edit_target()
            return
        self._show_form(monitor)

    def _missing_edit_target(self) -> None:
        self.reload_button.set_visibility(False)
        ui.notify("原任务已删除。草稿已保留，可复制表单内容。", type="warning")

    def _show_form(self, mon: Monitor | None) -> None:
        self.editing_monitor = mon
        self.reload_button.set_visibility(False)
        self.name_input.value = mon.name if mon else ""
        self.query_input.value = mon.keywords if mon else ""
        self.min_input.value = mon.min_price if mon else None
        self.max_input.value = mon.max_price if mon else None
        self.exclude_input.value = mon.filter_words if mon else ""
        self.mode_select.value = mon.keyword_mode if mon else "AND"
        self.requirement_input.value = (mon.ai_requirement or "") if mon else ""
        self.interval_select.value = mon.interval_minutes if mon else 30
        self.email_input.value = (mon.notification_email or "") if mon else ""
        self.enabled_switch.value = mon.enabled if mon else True
        self.monitor_dialog.open()

    def save(self) -> None:
        error = validate_monitor_form(
            self.name_input.value or "",
            self.query_input.value or "",
            self.min_input.value,
            self.max_input.value,
            self.mode_select.value or "",
        )
        if error:
            ui.notify(f"保存失败：{error}", type="negative")
            return
        fields = monitor_fields_from_form(
            self.name_input.value or "",
            self.query_input.value or "",
            self.min_input.value,
            self.max_input.value,
            self.mode_select.value or "AND",
            self.exclude_input.value or "",
            self.requirement_input.value or "",
            int(self.interval_select.value or 30),
            self.email_input.value or "",
            bool(self.enabled_switch.value),
        )
        snapshot = self.editing_monitor
        try:
            if snapshot is None:
                monitor_id = self.db.create_monitor(**fields)
                success = "已创建任务"
            else:
                monitor_id = snapshot.id
                if not self.db.update_monitor(monitor_id, **fields, expected=snapshot):
                    if self.db.get_monitor(monitor_id) is None:
                        self._missing_edit_target()
                    else:
                        self.reload_button.set_visibility(True)
                        ui.notify("此任务已在另一页修改。草稿已保留，可载入最新配置。", type="warning")
                    return
                success = "已保存任务"
        except sqlite3.IntegrityError:
            log.exception("保存 Monitor 失败（名称冲突）")
            ui.notify("保存失败：已经存在同名任务，请换一个名称。", type="negative")
            return

        self._apply_schedule(self.db.get_monitor(monitor_id))
        self.monitor_dialog.close()
        self.refresh()
        self.on_tasks_changed()
        ui.notify(f"{success}「{fields['name']}」")

    def _build_form(self) -> None:
        with ui.dialog() as self.monitor_dialog, ui.card().classes("w-[560px]"):
            ui.label("监控任务").classes("text-lg font-bold")
            self.name_input = ui.input("任务名称（必填，必须唯一）").props("dense")
            self.query_input = ui.input(
                "搜索关键词（必填，多个关键词请用中文逗号，分割）",
                placeholder="John Coltrane，A Love Supreme，CD",
            ).props("dense")
            self.mode_select = ui.select(
                {
                    "AND": "AND（全部关键词都要命中）",
                    "OR": "OR（任一关键词命中即可）",
                },
                label="关键词匹配模式",
                value="AND",
            ).props("dense")
            with ui.row().classes("w-full"):
                self.min_input = ui.number(
                    "最低价格 JPY（可选）", value=None, min=0, precision=0
                ).props("dense").classes("w-1/2")
                self.max_input = ui.number(
                    "最高价格 JPY（可选）", value=None, min=0, precision=0
                ).props("dense").classes("w-1/2")
            self.exclude_input = ui.input(
                "排除关键词（中文逗号，分割，任一命中即排除）",
                placeholder="LP，DVD，Blu-ray",
            ).props("dense")
            self.requirement_input = ui.textarea(
                "AI 语义要求（可选，留空时只使用本地规则）",
                placeholder="只保留 CD 格式的正式发行，不要黑胶、DVD、盗版或数字版本。",
            ).props("dense")
            self.interval_select = ui.select(
                INTERVAL_OPTIONS, label="扫描间隔", value=30
            ).props("dense")
            self.email_input = ui.input(
                "通知邮箱（可选，留空 = 使用默认收件人 SMTP_TO）",
                placeholder="留空则使用默认收件人",
            ).props("dense")
            self.enabled_switch = ui.switch("启用（保存后立即扫描一次）", value=True)
            with ui.row().classes("justify-end w-full"):
                ui.button("取消", on_click=self.monitor_dialog.close).props("flat")
                ui.button("保存", on_click=self.save)
            self.reload_button = ui.button(
                "放弃草稿并载入最新配置", on_click=self.reload_latest,
            ).props("flat")
            self.reload_button.set_visibility(False)
