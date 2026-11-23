import logging
from collections.abc import Callable

from nicegui import ui

from app.database import Database, Monitor
from app.exchange_rate import format_price_pair, format_rate, get_jpy_cny_rate, rate_status

from .confirmation import Confirmation

log = logging.getLogger("gui")

HISTORY_SORT_OPTIONS = {
    "published_desc": "发布时间：新 → 旧",
    "published_asc": "发布时间：旧 → 新",
    "price_desc": "价格：高 → 低",
    "price_asc": "价格：低 → 高",
}

HISTORY_PAGE_SIZE = 20

# Phase P1-followup: history view can show matched / rejected / all rows.
HISTORY_FILTER_OPTIONS = {
    "matched": "仅显示匹配商品",
    "rejected": "仅显示被拒绝商品",
    "all": "显示全部（匹配 + 被拒绝）",
}
HISTORY_FILTER_VALUES = {"matched": 1, "rejected": 0, "all": None}


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


class HistoryView:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.monitor_id: int | None = None
        self.history_page = 1
        self.ignored_page = 1
        self.sort_by = "published_desc"
        self.display_filter = "matched"
        self.rate: float | None = None
        self.rate_banner = format_rate(None)
        self.delete_confirmation = Confirmation("删除商品", "确认删除", "negative")
        self.ignore_confirmation = Confirmation("忽略商品", "确认忽略", "warning")
        self.unignore_confirmation = Confirmation("取消忽略", "确认取消忽略", "primary")

    def build(self) -> None:
        ui.separator()
        ui.label("匹配商品历史（按 Monitor 查看）").classes("text-xl font-bold")
        self.history_panel = ui.column().classes("w-full")
        ui.separator()
        ui.label("忽略商品（按 Monitor 查看）").classes("text-xl font-bold")
        self.ignored_panel = ui.column().classes("w-full")
        self.refresh()
        ui.timer(30, self.update_rate)

    async def update_rate(self) -> None:
        if self.monitor_id is None:
            return
        rate = await get_jpy_cny_rate()
        banner = _rate_banner_text(rate)
        if not self.history_panel.is_deleted and (rate, banner) != (self.rate, self.rate_banner):
            self.rate, self.rate_banner = rate, banner
            self.refresh()

    def refresh(self) -> None:
        monitors = self.db.list_monitors()
        selected = next((m for m in monitors if m.id == self.monitor_id), None)
        if selected is None:
            selected = monitors[0] if monitors else None
            self.monitor_id = selected.id if selected else None
            self.history_page = self.ignored_page = 1
        for panel, render in (
            (self.history_panel, self._render_history),
            (self.ignored_panel, self._render_ignored),
        ):
            panel.clear()
            with panel:
                if selected is None:
                    ui.label("暂无 Monitor，请先创建一个任务。").classes("text-gray-500")
                else:
                    render(selected, monitors)

    def select_monitor(self, monitor_id: int) -> None:
        self.monitor_id = monitor_id
        self.history_page = self.ignored_page = 1
        self.refresh()

    def set_sort(self, sort_by: str) -> None:
        self.sort_by = sort_by
        self.history_page = 1
        self.refresh()

    def set_filter(self, display_filter: str) -> None:
        self.display_filter = display_filter
        self.history_page = 1
        self.refresh()

    def change_history_page(self, delta: int) -> None:
        self.history_page += delta
        self.refresh()

    def change_ignored_page(self, delta: int) -> None:
        self.ignored_page += delta
        self.refresh()

    def ask_delete_product(self, monitor_id: int, mercari_id: str) -> None:
        self.delete_confirmation.open(
            "确定从当前 Monitor 中删除这个商品吗？\n\n"
            "删除后，如果以后再次搜索到该商品，它仍然可以被重新处理。",
            lambda: self._change_product(
                monitor_id, mercari_id, self.db.delete_monitor_product,
                "已从历史中删除（未来可重新处理）。",
            ),
        )

    def ask_ignore_product(self, monitor_id: int, mercari_id: str) -> None:
        self.ignore_confirmation.open(
            "确定要忽略这个商品吗？\n\n"
            "商品会从当前任务的历史移除。以后扫描将跳过该商品，直到取消忽略。",
            lambda: self._change_product(
                monitor_id, mercari_id, self.db.ignore_product, "已忽略该商品（永久跳过）。",
            ),
        )

    def ask_unignore_product(self, monitor_id: int, mercari_id: str) -> None:
        self.unignore_confirmation.open(
            "确定取消忽略这个商品吗？\n\n"
            "取消后，以后扫描到这个商品时可以重新经过筛选。",
            lambda: self._change_product(
                monitor_id, mercari_id, self.db.unignore_product,
                "已取消忽略（下次扫描可重新处理）。", ignored=True,
            ),
        )

    def _change_product(
        self,
        monitor_id: int,
        mercari_id: str,
        action: Callable[[int, str], None],
        success: str,
        *,
        ignored: bool = False,
    ) -> None:
        exists = (
            self.db.is_ignored(monitor_id, mercari_id) if ignored
            else self.db.product_decision(monitor_id, mercari_id) is not None
        )
        if not exists:
            ui.notify("该商品已从列表移除。", type="warning")
        else:
            try:
                action(monitor_id, mercari_id)
            except Exception:
                log.exception("商品操作失败: monitor=%s mercari=%s", monitor_id, mercari_id)
                ui.notify("操作失败，请查看日志。", type="negative")
                return
            ui.notify(success)
        self.refresh()

    def _render_history(self, monitor: Monitor, monitors: list[Monitor]) -> None:
        with ui.row().classes("items-center w-full"):
            ui.select(
                {str(m.id): m.name for m in monitors},
                value=str(monitor.id),  # option keys are strings
                label="Monitor",
                on_change=lambda e: self.select_monitor(int(e.value)),
            ).props("dense")
            ui.space()
            ui.select(
                HISTORY_SORT_OPTIONS,
                value=self.sort_by,
                label="排序",
                on_change=lambda e: self.set_sort(str(e.value)),
            ).props("dense")
            ui.select(
                HISTORY_FILTER_OPTIONS,
                value=self.display_filter,
                label="显示",
                on_change=lambda e: self.set_filter(str(e.value)),
            ).props("dense")
        ui.label(self.rate_banner).classes("text-xs text-gray-500")

        matched_value = HISTORY_FILTER_VALUES.get(self.display_filter, 1)
        ui.label(f"当前规则版本 v{monitor.rule_version}").classes("text-xs text-gray-500")

        total = self.db.count_monitor_products(monitor.id, matched=matched_value)
        total_pages = calculate_total_pages(total, HISTORY_PAGE_SIZE)
        self.history_page = clamp_page(self.history_page, total_pages)
        offset = (self.history_page - 1) * HISTORY_PAGE_SIZE
        rows = self.db.list_monitor_products(
            monitor.id,
            sort_by=self.sort_by,
            limit=HISTORY_PAGE_SIZE,
            offset=offset,
            matched=matched_value,
            include_decision=True,
        )

        empty_texts = {
            "matched": "暂无匹配商品",
            "rejected": "暂无被拒绝商品",
            "all": "暂无商品",
        }
        if not rows:
            ui.label(empty_texts.get(self.display_filter, "暂无商品")).classes("text-gray-500")
        else:
            for mercari_id, title, price, url, published_at, _found_at, matched, version in rows:
                with ui.row().classes("items-center w-full no-wrap"):
                    ui.link(title, target=url, new_tab=True)
                    jpy_text, cny_text = format_price_pair(price, self.rate)
                    with ui.column().classes("gap-0"):
                        ui.label(jpy_text).classes("text-sm font-medium")
                        ui.label(cny_text).classes("text-xs text-gray-500")
                    ui.label(
                        f"发布时间: {published_at or '未知'}"
                    ).classes("text-sm text-gray-500")
                    version_text = f"规则 v{version}" if version is not None else "版本未知"
                    verdict = "已匹配" if matched else "已拒绝"
                    if not matched and version != monitor.rule_version:
                        verdict = "旧规则拒绝，再次搜到后重判"
                    ui.label(f"{verdict} · {version_text}").classes("text-xs text-gray-500")
                    ui.space()
                    ui.button(
                        icon="delete",
                        color="negative",
                        on_click=lambda m=mercari_id: self.ask_delete_product(monitor.id, m),
                    ).props("flat dense").tooltip("从历史中删除（未来可重新处理）")
                    ui.button(
                        icon="block",
                        color="warning",
                        on_click=lambda m=mercari_id: self.ask_ignore_product(monitor.id, m),
                    ).props("flat dense").tooltip("忽略（移除历史并永久跳过）")

        with ui.row().classes("items-center justify-center w-full gap-2"):
            prev_button = ui.button("← 上一页", on_click=lambda: self.change_history_page(-1)) \
                .props("flat dense")
            ui.label(f"第 {self.history_page} / {total_pages} 页") \
                .classes("text-sm text-gray-600")
            next_button = ui.button("下一页 →", on_click=lambda: self.change_history_page(1)) \
                .props("flat dense")
        prev_button.set_enabled(self.history_page > 1)
        next_button.set_enabled(self.history_page < total_pages)

    def _render_ignored(self, monitor: Monitor, monitors: list[Monitor]) -> None:
        with ui.row().classes("items-center w-full"):
            ui.select(
                {str(m.id): m.name for m in monitors},
                value=str(monitor.id),  # option keys are strings
                label="Monitor",
                on_change=lambda e: self.select_monitor(int(e.value)),
            ).props("dense")
            ui.space()
            ui.label("忽略列表（最新忽略在前）").classes("text-sm text-gray-500")
        ui.label(self.rate_banner).classes("text-xs text-gray-500")

        total = self.db.count_ignored_products(monitor.id)
        total_pages = calculate_total_pages(total, HISTORY_PAGE_SIZE)
        self.ignored_page = clamp_page(self.ignored_page, total_pages)
        offset = (self.ignored_page - 1) * HISTORY_PAGE_SIZE
        rows = self.db.list_ignored_products(
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
                    jpy_text, cny_text = format_price_pair(price, self.rate)
                    with ui.column().classes("gap-0"):
                        ui.label(jpy_text).classes("text-sm font-medium")
                        ui.label(cny_text).classes("text-xs text-gray-500")
                    ui.label(f"忽略时间: {ignored_at or '未知'}") \
                        .classes("text-sm text-gray-500")
                    ui.space()
                    ui.button(
                        icon="undo",
                        color="primary",
                        on_click=lambda m=mercari_id: self.ask_unignore_product(monitor.id, m),
                    ).props("flat dense").tooltip("取消忽略（下次扫描可重新处理）")

        with ui.row().classes("items-center justify-center w-full gap-2"):
            prev_button = ui.button("← 上一页", on_click=lambda: self.change_ignored_page(-1)) \
                .props("flat dense")
            ui.label(f"第 {self.ignored_page} / {total_pages} 页") \
                .classes("text-sm text-gray-600")
            next_button = ui.button("下一页 →", on_click=lambda: self.change_ignored_page(1)) \
                .props("flat dense")
        prev_button.set_enabled(self.ignored_page > 1)
        next_button.set_enabled(self.ignored_page < total_pages)
