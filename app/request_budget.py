"""Process-wide scan request limits, shared by all tasks on one event loop."""

import asyncio
import os
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic


class RequestDeferred(Exception):
    """The scan or service became unavailable before sending a request."""


_request_allowed: ContextVar[Callable[[], bool] | None] = ContextVar(
    "scan_request_allowed", default=None,
)


@contextmanager
def guard_requests(allowed: Callable[[], bool]) -> Iterator[None]:
    previous = _request_allowed.get()
    token = _request_allowed.set(
        allowed if previous is None else lambda: previous() and allowed()
    )
    try:
        yield
    finally:
        _request_allowed.reset(token)


def check_request() -> None:
    allowed = _request_allowed.get()
    if allowed is not None and not allowed():
        raise RequestDeferred


class RequestBudget:
    def __init__(self, per_minute: int, *, clock=monotonic, sleep=asyncio.sleep):
        if per_minute <= 0:
            raise ValueError("Request limit must be a positive integer")
        self._limit = per_minute
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._queue = asyncio.Lock()

    @classmethod
    def from_env(cls, name: str, default: int) -> "RequestBudget":
        try:
            return cls(int(os.environ.get(name, str(default))))
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc

    async def acquire(self) -> None:
        check_request()
        # asyncio.Lock admits waiters in FIFO order. Keep it only while
        # selecting a start time, and release before doing network I/O.
        async with self._queue:
            while True:
                check_request()
                now = self._clock()
                while self._sent and now - self._sent[0] >= 60:
                    self._sent.popleft()
                if len(self._sent) < self._limit:
                    self._sent.append(now)
                    return
                await self._sleep(self._sent[0] + 60 - now)
