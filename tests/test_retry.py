"""Tests for ``clichefactory._retry.request_with_retries``.

These cover the retry layer in isolation. We use ``asyncio.run`` directly
(rather than ``pytest-asyncio``) to match the existing SDK test style and
avoid adding a dev dependency for one module.

Integration coverage of the service callsites that wrap this helper
(``_service.py`` / ``_upload.py``) lives alongside the existing
service-mode tests when those gain network fixtures; for now we lock
down the helper's contract here.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import httpx
import pytest

from clichefactory._retry import (
    DEFAULT_MAX_ATTEMPTS,
    RETRY_STATUS,
    _jittered_backoff,
    _parse_retry_after_seconds,
    request_with_retries,
)


class _FakeSleep:
    """Captures sleep calls so tests can assert backoff schedule without delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _resp(status_code: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """Build an httpx.Response detached from a real request."""
    return httpx.Response(status_code=status_code, headers=headers or {}, content=b"")


def _run(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    sleep: _FakeSleep,
    **kwargs: object,
) -> httpx.Response:
    return asyncio.run(request_with_retries(send, sleep=sleep, **kwargs))  # type: ignore[arg-type]


# --- Behavior tests ------------------------------------------------------------


def test_returns_immediately_on_2xx() -> None:
    sleep = _FakeSleep()
    calls = 0

    async def send() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _resp(200)

    resp = _run(send, sleep=sleep)
    assert resp.status_code == 200
    assert calls == 1
    assert sleep.calls == []


def test_does_not_retry_non_retryable_4xx() -> None:
    """401, 403, 404, 422 etc. fail fast — retrying just delays the error."""
    sleep = _FakeSleep()

    for status in (400, 401, 403, 404, 409, 422):
        calls = 0
        captured = status

        async def send() -> httpx.Response:
            nonlocal calls
            calls += 1
            return _resp(captured)

        resp = _run(send, sleep=sleep, max_attempts=4)
        assert resp.status_code == captured
        assert calls == 1, f"status {captured} should not retry"

    assert sleep.calls == [], "no sleeps should have been issued"


@pytest.mark.parametrize("status", sorted(RETRY_STATUS))
def test_retries_then_succeeds(status: int) -> None:
    sleep = _FakeSleep()
    statuses = iter([status, status, 200])
    calls = 0

    async def send() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _resp(next(statuses))

    resp = _run(send, sleep=sleep, max_attempts=4)
    assert resp.status_code == 200
    assert calls == 3
    assert len(sleep.calls) == 2


def test_returns_last_response_when_attempts_exhausted() -> None:
    """If every attempt is retryable, the last response is still returned —
    callers translate it into their own typed errors based on status."""
    sleep = _FakeSleep()
    calls = 0

    async def send() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _resp(503)

    resp = _run(send, sleep=sleep, max_attempts=3)
    assert resp.status_code == 503
    assert calls == 3
    # Sleep before retry, no sleep after the last attempt.
    assert len(sleep.calls) == 2


def test_honors_retry_after_seconds_header() -> None:
    sleep = _FakeSleep()
    statuses = iter([429, 200])

    async def send() -> httpx.Response:
        return _resp(next(statuses), headers={"Retry-After": "2.5"})

    resp = _run(send, sleep=sleep, max_attempts=3)
    assert resp.status_code == 200
    assert sleep.calls == [2.5]


def test_clamps_retry_after_to_cap() -> None:
    """A misbehaving upstream that asks for 1 hour gets capped."""
    sleep = _FakeSleep()
    statuses = iter([429, 200])

    async def send() -> httpx.Response:
        return _resp(next(statuses), headers={"Retry-After": "3600"})

    _run(send, sleep=sleep, max_attempts=3, retry_after_cap_s=10.0)
    assert sleep.calls == [10.0]


def test_falls_back_to_backoff_for_http_date_retry_after() -> None:
    """We don't parse HTTP-date form; we fall through to jittered backoff
    rather than getting stuck."""
    sleep = _FakeSleep()
    statuses = iter([503, 200])

    async def send() -> httpx.Response:
        return _resp(
            next(statuses), headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )

    resp = _run(
        send, sleep=sleep, max_attempts=3, base_delay_s=1.0, max_delay_s=4.0
    )
    assert resp.status_code == 200
    assert len(sleep.calls) == 1
    # Backoff is jittered: any value in [0, base*2**0] = [0, 1] for attempt 0.
    assert 0.0 <= sleep.calls[0] <= 1.0


def test_retries_on_transport_error() -> None:
    sleep = _FakeSleep()
    raises_left = 2

    async def send() -> httpx.Response:
        nonlocal raises_left
        if raises_left > 0:
            raises_left -= 1
            raise httpx.ConnectError("connection refused")
        return _resp(200)

    resp = _run(send, sleep=sleep, max_attempts=4)
    assert resp.status_code == 200
    assert len(sleep.calls) == 2


def test_propagates_transport_error_after_max_attempts() -> None:
    """Caller's typed-error wrapping (ServiceUnavailableError) sees the
    original httpx exception once retries are exhausted."""
    sleep = _FakeSleep()
    calls = 0

    async def send() -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.ConnectError):
        _run(send, sleep=sleep, max_attempts=3)
    assert calls == 3
    # One sleep before each retry, none after the last attempt.
    assert len(sleep.calls) == 2


def test_send_invoked_identically_on_each_attempt() -> None:
    """The retry layer must not mutate the request between attempts —
    that would break the service's idempotency replay (same key, same
    fingerprint)."""
    sleep = _FakeSleep()
    seen: list[object] = []
    sentinel = object()
    statuses = iter([502, 502, 200])

    async def send() -> httpx.Response:
        seen.append(sentinel)
        return _resp(next(statuses))

    _run(send, sleep=sleep, max_attempts=4)
    assert seen == [sentinel, sentinel, sentinel]


def test_max_attempts_one_means_no_retry() -> None:
    sleep = _FakeSleep()
    calls = 0

    async def send() -> httpx.Response:
        nonlocal calls
        calls += 1
        return _resp(503)

    resp = _run(send, sleep=sleep, max_attempts=1)
    assert resp.status_code == 503
    assert calls == 1
    assert sleep.calls == []


def test_default_max_attempts_is_reasonable() -> None:
    """Sanity check: the default isn't pathologically high."""
    assert 2 <= DEFAULT_MAX_ATTEMPTS <= 6


def test_invalid_max_attempts_raises() -> None:
    async def send() -> httpx.Response:
        return _resp(200)

    with pytest.raises(ValueError):
        asyncio.run(request_with_retries(send, max_attempts=0))


# --- Helper-level unit checks --------------------------------------------------


def test_parse_retry_after_seconds_accepts_numeric() -> None:
    assert _parse_retry_after_seconds("0") == 0.0
    assert _parse_retry_after_seconds("3") == 3.0
    assert _parse_retry_after_seconds("  7.5  ") == 7.5


def test_parse_retry_after_seconds_clamps_negative() -> None:
    assert _parse_retry_after_seconds("-5") == 0.0


def test_parse_retry_after_seconds_returns_none_for_unparseable() -> None:
    assert _parse_retry_after_seconds("") is None
    assert _parse_retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert _parse_retry_after_seconds("not-a-number") is None


def test_jittered_backoff_within_bounds() -> None:
    for attempt in range(5):
        for _ in range(20):
            delay = _jittered_backoff(attempt, base=0.5, cap=8.0)
            upper = min(8.0, 0.5 * (2 ** attempt))
            assert 0.0 <= delay <= upper


def test_jittered_backoff_zero_or_negative_cap_returns_zero() -> None:
    assert _jittered_backoff(3, base=0.0, cap=0.0) == 0.0
    assert _jittered_backoff(3, base=0.5, cap=0.0) == 0.0
