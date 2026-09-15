"""Offline tests for SMTP batch email. smtplib is mocked — nothing is sent."""

import asyncio
import smtplib

import pytest

from app.email import (
    build_batch_body,
    build_batch_subject,
    make_match_notifier,
    send_batch,
)
from app.mercari import SearchItem

ENV_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO")


def run(coro):
    return asyncio.run(coro)


class FakeSMTP:
    instances = []
    login_error = None
    send_error = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_calls = 0
        self.send_calls = 0
        self.quit_calls = 0
        self.close_calls = 0
        self.login_args = None
        self.sent_message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Mimic smtplib.SMTP.__exit__: try QUIT, then close.
        try:
            self.quit()
        except Exception:
            pass
        self.close()
        return False

    def quit(self):
        self.quit_calls += 1
        return (221, b"bye")

    def close(self):
        self.close_calls += 1

    def login(self, user, password):
        self.login_calls += 1
        self.login_args = (user, password)
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error

    def send_message(self, msg):
        self.send_calls += 1
        self.sent_message = msg
        if FakeSMTP.send_error is not None:
            raise FakeSMTP.send_error


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances.clear()
    FakeSMTP.login_error = None
    FakeSMTP.send_error = None
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.qq.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USERNAME", "hentilix@qq.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-auth-code")
    monkeypatch.setenv("SMTP_FROM", "hentilix@qq.com")
    monkeypatch.setenv("SMTP_TO", "hentilixrivery@outlook.com")


@pytest.fixture
def no_smtp_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make_item(item_id="m1", title="Built to Spill CD", price=1620):
    return SearchItem(
        id=item_id,
        title=title,
        price=price,
        url=f"https://jp.mercari.com/item/{item_id}",
    )


# ---------------------------------------------------------------- builders
def test_batch_subject_format():
    assert build_batch_subject("Built to Spill", 3) == "Mercari 新商品提醒｜Built to Spill｜3 件"


def test_batch_body_contains_all_items():
    items = [make_item("m1", "One", 1000), make_item("m2", "Two", 2000)]
    body = build_batch_body("Built to Spill", items)
    assert "任务：Built to Spill" in body
    assert "本次发现 2 件符合条件的新商品" in body
    assert "1. One" in body
    assert "价格：\u00a51,000" in body
    assert "https://jp.mercari.com/item/m1" in body
    assert "2. Two" in body
    assert "价格：\u00a52,000" in body
    assert "https://jp.mercari.com/item/m2" in body


def test_batch_body_no_price():
    item = SearchItem(id="m1", title="T", price=None, url="https://jp.mercari.com/item/m1")
    assert "no price" in build_batch_body("q", [item])


# ----------------------------------------------------------------- sending
def test_send_batch_empty_list_sends_nothing(smtp_env):
    run(send_batch("q", []))
    assert FakeSMTP.instances == []  # no connection at all


def test_send_batch_one_item_single_connection(smtp_env):
    run(send_batch("Built to Spill", [make_item("m1", "One", 1000)]))
    assert len(FakeSMTP.instances) == 1
    server = FakeSMTP.instances[0]
    assert server.host == "smtp.qq.com"
    assert server.port == 465
    assert server.login_calls == 1
    assert server.send_calls == 1
    assert server.quit_calls == 1
    assert server.login_args == ("hentilix@qq.com", "secret-auth-code")
    assert server.sent_message["From"] == "hentilix@qq.com"
    assert server.sent_message["To"] == "hentilixrivery@outlook.com"
    assert server.sent_message["Subject"] == "Mercari 新商品提醒｜Built to Spill｜1 件"


def test_send_batch_three_items_one_connection(smtp_env):
    items = [make_item("m1", "One"), make_item("m2", "Two"), make_item("m3", "Three")]
    run(send_batch("Built to Spill", items))
    assert len(FakeSMTP.instances) == 1  # ONE connection for all three
    server = FakeSMTP.instances[0]
    assert server.login_calls == 1
    assert server.send_calls == 1
    assert server.quit_calls == 1
    content = server.sent_message.get_content()  # non-ASCII -> decode
    for title in ("One", "Two", "Three"):
        assert title in content


def test_send_batch_missing_config(no_smtp_env):
    with pytest.raises(RuntimeError):
        run(send_batch("q", [make_item()]))
    assert FakeSMTP.instances == []


def test_send_batch_login_failure(smtp_env):
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, b"auth failed")
    with pytest.raises(RuntimeError):
        run(send_batch("q", [make_item()]))


def test_send_batch_send_failure(smtp_env):
    FakeSMTP.send_error = smtplib.SMTPException("send failed")
    with pytest.raises(RuntimeError):
        run(send_batch("q", [make_item()]))


def test_password_never_logged_on_success(smtp_env, caplog):
    run(send_batch("q", [make_item()]))
    assert "secret-auth-code" not in caplog.text


def test_password_never_logged_on_failure(smtp_env, caplog):
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, b"bad auth")
    with pytest.raises(RuntimeError):
        run(send_batch("q", [make_item()]))
    assert "secret-auth-code" not in caplog.text


# ---------------------------------------------------------------- notifier
def test_make_notifier_none_without_config(no_smtp_env):
    assert make_match_notifier("q") is None


def test_make_notifier_sends_one_batch_email(smtp_env):
    notifier = make_match_notifier("Built to Spill")
    assert notifier is not None
    run(notifier([make_item("m1", "One"), make_item("m2", "Two")]))
    assert len(FakeSMTP.instances) == 1
    server = FakeSMTP.instances[-1]
    assert server.sent_message["Subject"] == "Mercari 新商品提醒｜Built to Spill｜2 件"
    content = server.sent_message.get_content()
    assert "One" in content and "Two" in content


def test_make_notifier_recipient_override(smtp_env):
    notifier = make_match_notifier("q", recipient="chccrimson@gmail.com")
    assert notifier is not None
    run(notifier([make_item()]))
    assert FakeSMTP.instances[-1].sent_message["To"] == "chccrimson@gmail.com"


def test_make_notifier_default_recipient_from_env(smtp_env):
    notifier = make_match_notifier("q")  # no override -> SMTP_TO
    assert notifier is not None
    run(notifier([make_item()]))
    assert FakeSMTP.instances[-1].sent_message["To"] == "hentilixrivery@outlook.com"
