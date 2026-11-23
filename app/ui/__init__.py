"""NiceGUI pages. Each visit constructs its own controls and view state."""

from nicegui import ui

from app.database import Database
from app.scheduler import MonitorScheduler

from .history import HistoryView
from .monitors import MonitorsView
from .settings import build_settings


def register_pages(db: Database, scheduler: MonitorScheduler) -> None:
    @ui.page("/")
    def index() -> None:
        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
            ui.label("Mercari Japan Monitor").classes("text-3xl font-bold")
            ui.label("Scheduler: Running | 已启用任务按各自间隔自动扫描").classes("text-gray-500")
            build_settings()
            history = HistoryView(db)
            MonitorsView(db, scheduler, history.refresh).build()
            history.build()
