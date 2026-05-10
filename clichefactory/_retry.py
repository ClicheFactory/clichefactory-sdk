"""Bounded retry helper for service-mode HTTP calls.

Why this lives here
-------------------

Once the service is fronted by a load balancer with ≥2 replicas, transient
failures (connection resets during deploys, cooperative 429s from the
per-tenant rate limiter, the occasional 502 from the LB) become routine
rather than exceptional. The server stores idempotency records keyed by
``(tenant_id, idempotency_key, endpoint)`` precisely so a second attempt
can be replayed safely; the missing piece is a client that actually
retries while reusing the same key.

This module is the smallest helper that does that, scoped to the SDK's
own service-mode callsites:

* ``service_extract_via_canonical`` (POST ``/v1/canonical``) — the
  ``idempotency_key`` is already computed once from the request payload
  and lives inside the canonical envelope, so simply re-issuing the same
  body is enough.
* ``service_to_markdown`` (POST ``/v1/ocr/to-markdown``) and ``presign``
  (POST ``/v1/uploads/presign``) — these endpoints accept ``Idempotency-Key``
  as an HTTP header. Callers compute the key once before entering the
  retry loop and forward it on every attempt.
* ``upload_bytes`` — direct PUT to a presigned S3 URL. S3 PUTs are
  inherently idempotent on the URL+content, so retries only need to
  cover transport errors; no idempotency key is involved.

What we deliberately don't do
-----------------------------

* No client-wide retry transport: we keep retries scoped to the three
  service callsites so engine-level HTTP (Ollama, Gemini, OpenAI) stays
  unaffected. Those have their own provider-specific retry conventions.
* No exposed config knobs on the public ``factory`` / ``Client`` API.
  Reasonable defaults are baked in; we'll surface kwargs once a real
  user reports a tuning need.
* No retry on 4xx other than 408 / 425 / 429. Auth, validation, and
  not-found failures fail fast — retrying would just delay the error.
* No HTTP-date parsing for ``Retry-After`` (the seconds form covers the
  cases servers actually emit; HTTP-date adds parsing surface for no
  benefit here). When a server sends an HTTP-date we fall back to the
  jittered backoff schedule.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable

import httpx

# Statuses where a retry can plausibly succeed:
#   408 Request Timeout, 425 Too Early — rare but used by some LBs.
#   429 Too Many Requests — server-side rate limit, expects a backoff.
#   500 / 502 / 503 / 504 — transient server-side or LB hiccups.
# Any other 4xx is a client error and retrying just delays the failure.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 8.0
# Cap on a server-supplied ``Retry-After`` so a misconfigured upstream
# can't park a request indefinitely. 30 s comfortably covers a deploy
# rotation while keeping interactive callers responsive.
DEFAULT_RETRY_AFTER_CAP_S = 30.0


def _jittered_backoff(attempt: int, base: float, cap: float) -> float:
    """Full-jitter exponential backoff in [0, min(cap, base * 2**attempt)].

    Full jitter (vs. equal jitter or decorrelated jitter) keeps the
    implementation tiny and avoids the "thundering herd" failure mode
    where many clients line up to retry on the same boundary.
    """
    upper = min(cap, base * (2 ** attempt))
    if upper <= 0:
        return 0.0
    return random.uniform(0.0, upper)


def _parse_retry_after_seconds(value: str) -> float | None:
    """Parse the seconds form of RFC 7231 ``Retry-After``.

    Returns ``None`` for the HTTP-date form (we don't support it; callers
    fall through to jittered backoff) or any other unparseable input.
    Negative values clamp to zero.
    """
    s = value.strip()
    if not s:
        return None
    try:
        seconds = float(s)
    except ValueError:
        return None
    return max(0.0, seconds)


async def request_with_retries(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    retry_after_cap_s: float = DEFAULT_RETRY_AFTER_CAP_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Run ``send`` with bounded retries on transient failures.

    Parameters
    ----------
    send:
        A no-arg async callable that issues the HTTP request and returns
        the response. Re-invocation must produce the same logical request
        (same body, same idempotency key) so the server can replay a
        previously completed response. The caller is responsible for
        keeping the request shape identical across attempts; this helper
        does not introspect or rewrite the request.
    max_attempts:
        Hard upper bound on total attempts (including the first). Must
        be ≥ 1.
    base_delay_s, max_delay_s:
        Exponential-backoff parameters used when the server does not
        send a usable ``Retry-After`` header.
    retry_after_cap_s:
        Maximum sleep we'll accept from a server-sent ``Retry-After``.
        Protects callers from a misbehaving upstream that asks for
        impractical wait times.
    sleep:
        Awaitable sleep injection point; tests pass a fake to make the
        backoff loop deterministic.

    Returns
    -------
    The final :class:`httpx.Response`. If the last attempt was a
    retryable status, the response is still returned (callers raise
    their own typed errors based on status); if the last attempt raised
    a transport exception, that exception propagates.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    response: httpx.Response | None = None

    for attempt in range(max_attempts):
        try:
            response = await send()
        except httpx.RequestError:
            # Connect/read timeout, DNS failure, connection reset, etc.
            # If we've exhausted attempts, let the caller see the original
            # exception (they translate it into ServiceUnavailableError).
            if attempt == max_attempts - 1:
                raise
            await sleep(_jittered_backoff(attempt, base_delay_s, max_delay_s))
            continue

        if response.status_code in RETRY_STATUS and attempt < max_attempts - 1:
            delay = _delay_from_response(
                response,
                attempt=attempt,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
                retry_after_cap_s=retry_after_cap_s,
            )
            await sleep(delay)
            continue

        return response

    # Unreachable in practice: the transport-error branch raises on the
    # final attempt and the success/non-retryable-status branch returns.
    # Asserted to silence type-checkers and surface logic regressions
    # loudly if the loop is ever refactored.
    assert response is not None
    return response


def _delay_from_response(
    response: httpx.Response,
    *,
    attempt: int,
    base_delay_s: float,
    max_delay_s: float,
    retry_after_cap_s: float,
) -> float:
    """Compute the sleep before the next attempt for a retryable status.

    Prefers a server-sent ``Retry-After`` (clamped) over the jittered
    backoff schedule. Falls back to backoff when the header is absent
    or in HTTP-date form.
    """
    header = response.headers.get("Retry-After")
    if header:
        parsed = _parse_retry_after_seconds(header)
        if parsed is not None:
            return min(parsed, retry_after_cap_s)
    return _jittered_backoff(attempt, base_delay_s, max_delay_s)


__all__ = [
    "DEFAULT_BASE_DELAY_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY_S",
    "DEFAULT_RETRY_AFTER_CAP_S",
    "RETRY_STATUS",
    "request_with_retries",
]
