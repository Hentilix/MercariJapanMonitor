"""DeepSeek calls and shared availability for monitor scans.

Verified against the current official DeepSeek API docs (api-docs.deepseek.com):
  * endpoint: https://api.deepseek.com/chat/completions
  * model:    deepseek-flash (legacy "deepseek-chat" has been retired)
  * thinking mode is ON by default -> disabled explicitly here to save tokens
"""

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from time import monotonic, time

import httpx

from app.mercari import ItemDetail
from app.request_budget import RequestBudget, RequestDeferred, guard_requests

log = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
API_KEY_ENV = "DEEPSEEK_API_KEY_FOR_MJM"
COOLDOWN_SECONDS = 300


def get_api_key() -> str:
    return os.environ.get(API_KEY_ENV, "").strip()


class DeepSeekUnavailable(Exception):
    def __init__(self, reason: str, delay: float = COOLDOWN_SECONDS):
        super().__init__(reason)
        self.delay = delay


def _retry_delay(retry_after: str) -> float:
    try:
        delay = int(retry_after)
    except ValueError:
        try:
            delay = parsedate_to_datetime(retry_after).timestamp() - time()
        except (ValueError, TypeError, OverflowError, OSError):
            return COOLDOWN_SECONDS
    return max(COOLDOWN_SECONDS, delay)


@dataclass
class _Availability:
    key: str
    reason: str = ""
    retry_at: float = 0
    probing: bool = False
    failures: int = 0


class DeepSeekClient:
    """Own the availability state for the key shared by all monitors."""

    def __init__(self, http: httpx.AsyncClient, budget: RequestBudget, *, clock=monotonic):
        self.http = http
        self.budget = budget
        self.clock = clock
        self._availability = _Availability("")

    def make_judge(self, requirement: str) -> "DeepSeekJudge | None":
        key = get_api_key()
        if key != self._availability.key:
            self._availability = _Availability(key)
        if requirement and key:
            return DeepSeekJudge(self, self._availability, requirement)
        return None


class DeepSeekJudge:
    def __init__(self, client: DeepSeekClient, availability: _Availability, requirement: str):
        self._client = client
        self._availability = availability
        self._requirement = requirement

    def _key_is_current(self) -> bool:
        return (
            self._availability is self._client._availability
            and self._availability.key == get_api_key()
        )

    def pause_reason(self) -> str:
        state = self._availability
        if not self._key_is_current():
            return "DeepSeek API Key 已变更，下轮继续"
        if state.retry_at > self._client.clock():
            return state.reason
        if state.probing:
            return "DeepSeek 正在试判"
        return ""

    @contextmanager
    def attempt(self):
        if self.pause_reason():
            raise RequestDeferred
        state = self._availability
        probe = bool(state.reason)
        if probe:
            state.probing = True
        failures = state.failures

        def allowed() -> bool:
            return self._key_is_current() and state.failures == failures

        async def judge(detail: ItemDetail) -> bool | None:
            await self._client.budget.acquire()
            try:
                verdict = await _request_verdict(
                    self._client.http, self._requirement, detail, state.key,
                )
            except DeepSeekUnavailable as exc:
                state.failures += 1
                state.reason = str(exc)
                state.retry_at = max(state.retry_at, self._client.clock() + exc.delay)
                log.warning("%s，暂停 AI 判定", exc)
                raise
            if probe and allowed():
                state.reason = ""
                state.retry_at = 0
            if not self._key_is_current():
                raise RequestDeferred
            return verdict

        try:
            with guard_requests(allowed):
                yield judge
        finally:
            if probe:
                state.probing = False

SYSTEM_PROMPT = (
    "你是一个严格的商品筛选器。"
    "根据给定的商品信息判断它是否满足用户要求。"
    "如果满足，只输出 true；如果不满足，只输出 false。"
    "不要输出任何其他内容，不要解释理由。"
)


def build_user_prompt(special_requirement: str, detail: ItemDetail) -> str:
    """Build the smallest useful prompt from the extracted fields."""
    return "\n".join(
        [
            f"用户要求: {special_requirement}",
            f"标题: {detail.title or ''}",
            f"描述: {detail.description or ''}",
            f"成色: {detail.condition or ''}",
            f"分类: {detail.category or ''}",
        ]
    )


def parse_bool_response(text: str | None) -> bool | None:
    """Strict parser: only 'true'/'false' (after stripping whitespace).

    Anything else — 'yes', '符合', 'True', 'true because...' — is None
    (invalid), never silently converted to a match.
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


async def _request_verdict(
    http: httpx.AsyncClient,
    special_requirement: str,
    detail: ItemDetail,
    api_key: str,
) -> bool | None:
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(special_requirement, detail)},
        ],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
        # Thinking is enabled by default on deepseek-flash; a strict
        # true/false filter needs no reasoning tokens.
        "thinking": {"type": "disabled"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = await http.post(DEEPSEEK_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise DeepSeekUnavailable(f"DeepSeek 网络故障（{type(exc).__name__}）") from exc

    status = response.status_code
    if status in (401, 402, 403, 429) or 500 <= status < 600:
        retry_after = getattr(response, "headers", {}).get("Retry-After", "")
        raise DeepSeekUnavailable(f"DeepSeek HTTP {status}", _retry_delay(retry_after))

    if response.status_code != 200:
        log.error(
            "DeepSeek API error: HTTP %d: %s",
            response.status_code,
            response.text[:200],
        )
        return None

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        log.error("DeepSeek response parse failed: %s", exc)
        return None

    result = parse_bool_response(content)
    if result is None:
        log.error("DeepSeek returned an invalid reply: %r", content)
    return result
