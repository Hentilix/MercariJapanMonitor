from collections.abc import Callable

from nicegui import ui


class Confirmation:
    def __init__(self, title: str, confirm_text: str, color: str) -> None:
        self._action: Callable[[], None] | None = None
        with ui.dialog() as self.dialog, ui.card():
            ui.label(title).classes("text-lg font-bold")
            self.message = ui.label("").classes("whitespace-pre-line")
            with ui.row().classes("justify-end w-full"):
                ui.button("取消", on_click=self.dialog.close).props("flat")
                ui.button(confirm_text, color=color, on_click=self._confirm)
        self.dialog.on_value_change(self._on_visibility_changed)

    def open(self, message: str, action: Callable[[], None]) -> None:
        self._action = action
        self.message.set_text(message)
        self.dialog.open()

    def _on_visibility_changed(self, event) -> None:
        if not event.value:
            self._action = None

    def _confirm(self) -> None:
        action = self._action
        self.dialog.close()
        if action is not None:
            action()
