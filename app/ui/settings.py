import os

from nicegui import ui

from app.deepseek import API_KEY_ENV, get_api_key
from app.email import ENV_KEYS

SMTP_DEFAULTS = {
    "SMTP_HOST": "smtp.qq.com",
    "SMTP_PORT": "465",
    "SMTP_USERNAME": "hentilix@qq.com",
    "SMTP_FROM": "hentilix@qq.com",
    "SMTP_TO": "chccrimson@gmail.com",
}


def smtp_status_text() -> str:
    """Three-state SMTP config status, using the SAME 6 variables that
    app.email actually requires (ENV_KEYS) — one source of truth."""
    missing = [key for key in ENV_KEYS if not os.environ.get(key)]
    if not missing:
        return "SMTP：已配置"
    if len(missing) == len(ENV_KEYS):
        return "SMTP：未配置"
    return "SMTP：配置不完整（缺：" + ", ".join(missing) + "）"


def deepseek_status_text() -> str:
    return (
        "DeepSeek：已设置"
        if get_api_key()
        else "DeepSeek：未设置"
    )


def config_status_text() -> str:
    return f"{deepseek_status_text()}    {smtp_status_text()}"


def _save_settings(
    status,
    key_input, host_input, port_input, user_input, pass_input,
    from_input, to_input,
) -> None:
    pairs = [
        (API_KEY_ENV, key_input.value),
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
    status.refresh()


def build_settings() -> None:
    @ui.refreshable
    def settings_status() -> None:
        ui.label(config_status_text()).classes("text-sm text-gray-600")

    settings_status()
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
                on_click=lambda: _save_settings(
                    settings_status,
                    key_input, host_input, port_input, user_input,
                    pass_input, from_input, to_input,
                ),
            )
