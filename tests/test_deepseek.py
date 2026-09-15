"""Offline tests for the DeepSeek caller. No real API calls."""

import asyncio

import httpx
import pytest

from app.deepseek import (
    DEEPSEEK_URL,
    build_user_prompt,
    judge_item,
    parse_bool_response,
)
from app.mercari import ItemDetail


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- parser
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True),
        ("false", False),
        (" true\n", True),
        ("\nfalse ", False),
        ("yes", None),
        ("True", None),  # strict: lowercase only
        ("true because this is a CD", None),
        ("符合", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_bool_response_strict(raw, expected):
    assert parse_bool_response(raw) == expected


# ---------------------------------------------------------------- prompt
def test_prompt_contains_requirement_and_all_fields():
    detail = ItemDetail(title="T", description="D", condition="C", category="K")
    prompt = build_user_prompt("必须是 CD", detail)
    assert "必须是 CD" in prompt
    for label, value in (("标题", "T"), ("描述", "D"), ("成色", "C"), ("分类", "K")):
        assert f"{label}: {value}" in prompt


def test_prompt_handles_empty_fields():
    detail = ItemDetail(title="", description=None, condition=None, category=None)
    prompt = build_user_prompt("req", detail)
    assert "req" in prompt
    assert len(prompt.splitlines()) == 5  # no extra fields ever added


# ---------------------------------------------------------------- judge
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        if self._error is not None:
            raise self._error
        return self._response


DETAIL = ItemDetail(title="T", description="D", condition="C", category="K")


def make_response(content):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY_FOR_MJM", "test-key")


def test_judge_returns_true(api_key):
    http = FakeHttp(make_response("true"))
    assert run(judge_item(http, "req", DETAIL)) is True


def test_judge_returns_false(api_key):
    http = FakeHttp(make_response("false"))
    assert run(judge_item(http, "req", DETAIL)) is False


def test_judge_invalid_reply_is_none(api_key):
    http = FakeHttp(make_response("yes, it matches"))
    assert run(judge_item(http, "req", DETAIL)) is None


def test_judge_http_error_is_none(api_key):
    http = FakeHttp(FakeResponse(500, text="server error"))
    assert run(judge_item(http, "req", DETAIL)) is None


def test_judge_bad_body_is_none(api_key):
    http = FakeHttp(FakeResponse(200, {"unexpected": "shape"}))
    assert run(judge_item(http, "req", DETAIL)) is None


def test_judge_network_error_is_none(api_key):
    http = FakeHttp(error=httpx.ConnectError("boom"))
    assert run(judge_item(http, "req", DETAIL)) is None


def test_judge_missing_api_key_is_none(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY_FOR_MJM", raising=False)
    http = FakeHttp(make_response("true"))
    assert run(judge_item(http, "req", DETAIL)) is None
    assert http.calls == []  # no request was even attempted


def test_judge_payload_is_minimal_and_correct(api_key):
    http = FakeHttp(make_response("true"))
    run(judge_item(http, "req", DETAIL))
    url, payload, headers = http.calls[0]
    assert url == DEEPSEEK_URL
    assert headers["Authorization"] == "Bearer test-key"
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == 8
    assert payload["temperature"] == 0
    assert payload["thinking"] == {"type": "disabled"}
    user_content = payload["messages"][1]["content"]
    for field in ("标题", "描述", "成色", "分类"):
        assert field in user_content
    assert "url" not in user_content and "id" not in user_content
