"""Minimal DeepSeek caller: item fields in, strict true/false out.

Direct HTTP via httpx (OpenAI-compatible API). No SDK, no abstractions.

Verified against the current official DeepSeek API docs (api-docs.deepseek.com):
  * endpoint: https://api.deepseek.com/chat/completions
  * model:    deepseek-flash (legacy "deepseek-chat" has been retired)
  * thinking mode is ON by default -> disabled explicitly here to save tokens
"""

import logging
import os

import httpx

from app.mercari import ItemDetail

log = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"

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
    if text is None:
        return None
    value = text.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


async def judge_item(
    http: httpx.AsyncClient,
    special_requirement: str,
    detail: ItemDetail,
    *,
    model: str = DEFAULT_MODEL,
) -> bool | None:
    """Judge one item. Returns True/False, or None on any failure.

    None (API error, bad status, unparseable reply, missing key) is NEVER
    treated as a match.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY_FOR_MJM")
    if not api_key:
        log.error("DEEPSEEK_API_KEY_FOR_MJM environment variable is not set")
        return None

    payload = {
        "model": model,
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
        log.error("DeepSeek request failed: %s", exc)
        return None

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
