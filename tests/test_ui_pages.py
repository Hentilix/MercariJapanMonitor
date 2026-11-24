"""Exercise the same NiceGUI route from tabs sharing one browser session."""

import asyncio
import sys
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.storage import Storage
from nicegui.testing import User, UserInteraction, user_simulation

from app.database import Database
from app.mercari import SearchPage
from app.scanner import scan_monitor
from app.scheduler import MonitorScheduler, ScanOutcome, ScanStatus
from app.ui import register_pages


def click_task_action(user, task_name, icon):
    card = next(
        element for element in user.client.elements.values()
        if isinstance(element, ui.card)
        and any(isinstance(child, ui.label) and child.text == task_name for child in element.descendants())
    )
    button = next(
        child for child in card.descendants()
        if isinstance(child, ui.button) and child.props.get("icon") == icon
    )
    UserInteraction(user, [button], None).click()


def open_dialog(user):
    return next(element for element in user.client.elements.values()
                if isinstance(element, ui.dialog) and element.value)


def click_dialog(user, text):
    button = next(child for child in open_dialog(user).descendants()
                  if isinstance(child, ui.button) and child.text == text)
    UserInteraction(user, [button], None).click()


def form_name(user):
    return next(child for child in open_dialog(user).descendants()
                if isinstance(child, ui.input) and child.props.get("label", "").startswith("任务名称"))


def selects(user, label):
    return [element for element in user.client.elements.values()
            if isinstance(element, ui.select) and element.props.get("label") == label]


def choose(user, label, value):
    with user:
        selects(user, label)[0].set_value(value)


def click_product_action(user, title, icon):
    link = next(element for element in user.client.elements.values()
                if isinstance(element, ui.link) and element.text == title)
    button = next(child for child in link.parent_slot.parent.descendants()
                  if isinstance(child, ui.button) and child.props.get("icon") == icon)
    UserInteraction(user, [button], None).click()


def add_product(db, monitor_id, item):
    db.add_monitor_product(monitor_id, mercari_id=item, title=item, price=100, url="https://example.test")


@pytest_asyncio.fixture
async def page_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Storage, "path", tmp_path)
    monkeypatch.setattr("app.ui.history.get_jpy_cny_rate", AsyncMock(return_value=None))
    # NiceGUI's simulation unloads modules that register pages on exit.
    for name in ("app", "app.ui"):
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    db = Database(":memory:")
    scheduler = Mock()
    try:
        async with user_simulation() as tab_a:
            register_pages(db, scheduler)
            yield db, scheduler, tab_a, User(tab_a.http_client)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_deleting_a_task_confirms_the_target_in_its_own_tab(page_session):
    db, scheduler, tab_a, tab_b = page_session
    alpha = db.create_monitor(name="alpha", keywords="CD", enabled=False)
    beta = db.create_monitor(name="beta", keywords="LP", enabled=False)
    await tab_a.open("/")
    click_task_action(tab_a, "alpha", "delete")
    await tab_b.open("/")
    click_task_action(tab_b, "beta", "delete")

    click_dialog(tab_a, "确认删除")

    assert db.get_monitor(alpha) is None
    assert db.get_monitor(beta) is not None
    assert open_dialog(tab_b).value
    scheduler.remove_monitor.assert_called_once_with(alpha)


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True], ids=["create", "edit"])
async def test_monitor_form_requires_a_keyword_before_saving(page_session, editing):
    db, scheduler, tab_a, _ = page_session
    if editing:
        db.create_monitor(name="alpha", keywords="LP", ai_requirement="CD", enabled=False)
    before = db.list_monitors()
    await tab_a.open("/")
    if editing:
        click_task_action(tab_a, "alpha", "edit")
    else:
        tab_a.find("新建任务").click()
        form_name(tab_a).value = "alpha"
        for field in open_dialog(tab_a).descendants():
            if isinstance(field, ui.textarea):
                field.value = "CD"
            if isinstance(field, ui.switch):
                field.value = False
    keywords = next(
        field for field in open_dialog(tab_a).descendants()
        if isinstance(field, ui.input) and field.props.get("label", "").startswith("搜索关键词")
    )
    keywords.value = ", ，"
    click_dialog(tab_a, "保存")

    assert db.list_monitors() == before
    scheduler.schedule.assert_not_called()
    scheduler.run_monitor_now.assert_not_called()
    assert tab_a.notify.contains("请至少填写一个搜索关键词。")
    assert open_dialog(tab_a).value
    assert keywords.value == ", ，"

    keywords.value = "，CD,,"
    click_dialog(tab_a, "保存")
    saved, = db.list_monitors()
    assert saved.keywords == "，CD,,"
    if editing:
        assert saved.id == before[0].id
    scheduler.schedule.assert_called_once_with(saved)


@pytest.mark.asyncio
async def test_edit_conflict_preserves_draft_and_allows_reload(page_session):
    db, scheduler, tab_a, tab_b = page_session
    monitor_id = db.create_monitor(name="alpha", keywords="CD", ai_requirement="CD", enabled=False)
    await tab_a.open("/")
    await tab_b.open("/")
    tab_a.find("新建任务").click()
    assert not any(isinstance(e, ui.dialog) and e.value for e in tab_b.client.elements.values())
    click_dialog(tab_a, "取消")
    click_task_action(tab_a, "alpha", "edit")
    click_task_action(tab_b, "alpha", "edit")
    form_name(tab_a).value = "saved"
    form_name(tab_b).value = "draft"

    db.set_last_scan(monitor_id, "background scan")
    click_dialog(tab_a, "保存")
    assert db.get_monitor(monitor_id).name == "saved"
    click_dialog(tab_b, "保存")

    assert db.get_monitor(monitor_id).name == "saved"
    assert form_name(tab_b).value == "draft"
    assert tab_b.notify.contains("草稿已保留")
    scheduler.schedule.assert_called_once()
    click_dialog(tab_b, "放弃草稿并载入最新配置")
    assert form_name(tab_b).value == "saved"
    form_name(tab_b).value = "new draft"
    click_task_action(tab_a, "saved", "delete")
    click_dialog(tab_a, "确认删除")
    click_dialog(tab_b, "保存")
    assert tab_b.notify.contains("原任务已删除")
    assert form_name(tab_b).value == "new draft"
    scheduler.schedule.assert_called_once()


@pytest.mark.asyncio
async def test_views_keep_separate_selection_sort_filters_and_pages(page_session):
    db, _, tab_a, tab_b = page_session
    alpha = db.create_monitor(name="alpha", keywords="CD", enabled=False)
    beta = db.create_monitor(name="beta", keywords="LP", enabled=False)
    for index in range(21):
        add_product(db, beta, f"history-{index}")
        add_product(db, beta, f"ignored-{index}")
        db.ignore_product(beta, f"ignored-{index}")

    await tab_a.open("/")
    await tab_b.open("/")
    choose(tab_a, "Monitor", str(beta))
    choose(tab_a, "排序", "price_asc")
    choose(tab_a, "显示", "all")
    assert [select.value for select in selects(tab_a, "Monitor")] == [str(beta)] * 2
    assert [select.value for select in selects(tab_b, "Monitor")] == [str(alpha)] * 2
    assert selects(tab_b, "排序")[0].value == "published_desc"
    assert selects(tab_b, "显示")[0].value == "matched"

    def page_buttons(user):
        return [e for e in user.client.elements.values()
                if isinstance(e, ui.button) and e.text == "下一页 →"]

    UserInteraction(tab_a, [page_buttons(tab_a)[0]], None).click()
    assert not page_buttons(tab_a)[0].enabled and page_buttons(tab_a)[1].enabled
    UserInteraction(tab_a, [page_buttons(tab_a)[1]], None).click()
    assert not any(button.enabled for button in page_buttons(tab_a))
    await tab_a.should_see("第 2 / 2 页")
    await tab_b.should_not_see("第 2 / 2 页")

    original_client = tab_a.client
    await tab_a.open("/")
    original_client.delete()
    assert [select.value for select in selects(tab_a, "Monitor")] == [str(alpha)] * 2
    assert selects(tab_a, "排序")[0].value == "published_desc"
    assert selects(tab_a, "显示")[0].value == "matched"
    choose(tab_b, "Monitor", str(beta))
    click_task_action(tab_a, "beta", "delete")
    click_dialog(tab_a, "确认删除")
    assert selects(tab_b, "Monitor")[0].value == str(beta)
    tab_a.client.delete()
    choose(tab_b, "显示", "all")
    assert [select.value for select in selects(tab_b, "Monitor")] == [str(alpha)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("icon", "button"), [
    ("delete", "确认删除"), ("block", "确认忽略"), ("undo", "确认取消忽略"),
])
async def test_product_confirmation_keeps_monitor_and_product_ids(page_session, icon, button):
    db, _, tab_a, tab_b = page_session
    alpha = db.create_monitor(name="alpha", keywords="CD", enabled=False)
    beta = db.create_monitor(name="beta", keywords="LP", enabled=False)
    add_product(db, alpha, "album-a")
    add_product(db, beta, "album-b")
    if icon == "undo":
        db.ignore_product(alpha, "album-a")
        db.ignore_product(beta, "album-b")
    await tab_a.open("/")
    click_product_action(tab_a, "album-a", icon)
    await tab_b.open("/")
    choose(tab_b, "Monitor", str(beta))
    click_product_action(tab_b, "album-b", icon)

    click_dialog(tab_a, button)
    if icon == "undo":
        assert not db.is_ignored(alpha, "album-a")
        assert db.is_ignored(beta, "album-b")
    else:
        assert db.product_decision(alpha, "album-a") is None
        assert db.product_decision(beta, "album-b") is not None
        assert db.is_ignored(alpha, "album-a") == (icon == "block")


@pytest.mark.asyncio
async def test_manual_scan_displays_failure_then_current_result(page_session):
    db, scheduler, tab_a, _ = page_session
    monitor_id = db.create_monitor(name="alpha", keywords="CD", enabled=False)
    db.set_last_scan(monitor_id, "old summary")

    async def complete(target):
        db.set_last_scan(target, "new summary")
        return "new summary"

    async def wait_for_notification(message):
        async with asyncio.timeout(2):
            while not tab_a.notify.contains(message):
                await asyncio.sleep(0.01)

    scan = AsyncMock(side_effect=RuntimeError("scan failed"))
    real_scheduler = MonitorScheduler(scan)
    scheduler.run_monitor_now = real_scheduler.run_monitor_now
    scheduler.is_running = real_scheduler.is_running
    await tab_a.open("/")
    click_task_action(tab_a, "alpha", "play_arrow")
    await wait_for_notification("扫描失败，请查看日志。")
    assert not tab_a.notify.contains("扫描完成")
    assert db.get_monitor(monitor_id).last_result == "old summary"

    scan.side_effect = complete
    click_task_action(tab_a, "alpha", "play_arrow")
    await wait_for_notification("扫描完成: new summary")
    assert scan.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "edit"])
async def test_manual_scan_displays_warning_for_invalidated_result(page_session, change):
    db, scheduler, tab_a, _ = page_session
    monitor_id = db.create_monitor(name="alpha", keywords="CD", enabled=False)
    db.set_last_scan(monitor_id, "old summary")
    monitor = db.get_monitor(monitor_id)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def search_page(*args, **kwargs):
        started.set()
        await finish.wait()
        return SearchPage([], None)

    async def scan(target):
        assert target == monitor_id
        client = AsyncMock()
        client.search_page.side_effect = search_page
        return (await scan_monitor(client, db, monitor)).summary

    real_scheduler = MonitorScheduler(scan)
    scheduler.run_monitor_now = real_scheduler.run_monitor_now
    scheduler.is_running = real_scheduler.is_running
    await tab_a.open("/")
    click_task_action(tab_a, "alpha", "play_arrow")
    try:
        await asyncio.wait_for(started.wait(), 2)
        outcome = await real_scheduler.run_monitor_now(monitor_id)
        assert outcome.status is ScanStatus.ALREADY_RUNNING
        click_task_action(tab_a, "alpha", "play_arrow")
        await asyncio.sleep(0)
        assert tab_a.notify.contains("该任务正在扫描中")
        if change == "delete":
            db.delete_monitor(monitor_id)
        else:
            assert db.update_monitor(
                monitor_id, name=monitor.name, keywords="LP", keyword_mode=monitor.keyword_mode,
                min_price=monitor.min_price, max_price=monitor.max_price,
                filter_words=monitor.filter_words, ai_requirement=monitor.ai_requirement,
                interval_minutes=monitor.interval_minutes, notification_email=monitor.notification_email,
                enabled=monitor.enabled,
            )
    finally:
        finish.set()
        await real_scheduler.stop()
    assert tab_a.notify.contains("本次结果已失效")
    assert not tab_a.notify.contains("扫描完成")


@pytest.mark.asyncio
async def test_closing_tab_releases_timers_without_stopping_scan(page_session, caplog):
    db, scheduler, tab_a, tab_b = page_session
    monitor_id = db.create_monitor(name="alpha", keywords="CD", enabled=False)
    started = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()

    async def scan(target):
        started.set()
        await finish.wait()
        db.set_last_scan(target, "scan completed")
        completed.set()
        return ScanOutcome(ScanStatus.COMPLETED, "scan completed")

    scheduler.run_monitor_now = AsyncMock(side_effect=scan)
    scheduler.is_running.return_value = False
    await tab_a.open("/")
    await tab_b.open("/")
    timers = [e for e in tab_a.client.elements.values() if isinstance(e, ui.timer)]
    click_task_action(tab_a, "alpha", "play_arrow")
    try:
        await asyncio.wait_for(started.wait(), 2)
        tab_a.client.delete()
        assert timers and all(timer.is_deleted for timer in timers)
    finally:
        finish.set()
    await asyncio.wait_for(completed.wait(), 2)
    scheduler.run_monitor_now.assert_awaited_once_with(monitor_id)
    assert db.get_monitor(monitor_id).last_result == "scan completed"
    assert not caplog.records or all(record.levelname != "ERROR" for record in caplog.records)
    assert not tab_b.notify.contains("扫描完成")
