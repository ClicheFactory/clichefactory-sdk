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

In-flight 409s
--------------

A second flavor of "retryable" exists for service endpoints: HTTP 409
with the server's "already in flight" semantics. The aio-server returns
this when a previous attempt for the same ``(tenant, idempotency_key,
endpoint)`` is still running — typically because a test/script reran
before the previous OCR / extract job finished, or a transport hiccup
made the client give up on a response while the server kept working.

We detect this case via :func:`is_in_flight_conflict` (Retry-After
header present, or response body ``{"error": "already_in_flight"}``)
and poll with a separate, more generous budget: long OCR jobs run for
multiple minutes, so a 30 s cap × 4 attempts isn't enough to outlast
them. Plain client 409s (e.g. fingerprint mismatch on the same key)
still fail fast — we don't blanket-retry the status code.

If polling is exhausted while the server is still working, the caller
sees a typed :class:`~clichefactory.errors.AlreadyInFlightError` rather
than a generic parsing/extraction failure, with a hint that the request
is safe to retry later.

What we deliberately don't do
-----------------------------

* No client-wide retry transport: we keep retries scoped to the three
  service callsites so engine-level HTTP (Ollama, Gemini, OpenAI) stays
  unaffected. Those have their own provider-specific retry conventions.
* No exposed config knobs on the public ``factory`` / ``Client`` API.
  Reasonable defaults are baked in; we'll surface kwargs once a real
  user reports a tuning need.
* No retry on 4xx other than 408 / 425 / 429 and the in-flight 409
  variant. Auth, validation, and not-found failures fail fast —
  retrying would just delay the error.
* No HTTP-date parsing for ``Retry-After`` (the seconds form covers the
  cases servers actually emit; HTTP-date adds parsing surface for no
  benefit here). When a server sends an HTTP-date we fall back to the
  jittered backoff schedule.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Awaitable, Callable

import httpx

# Statuses where a retry can plausibly succeed:
#   408 Request Timeout, 425 Too Early — rare but used by some LBs.
#   429 Too Many Requests — server-side rate limit, expects a backoff.
#   500 / 502 / 503 / 504 — transient server-side or LB hiccups.
# Any other 4xx is a client error and retrying just delays the failure.
# 409 is handled separately — see ``is_in_flight_conflict``.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 8.0
# Cap on a server-supplied ``Retry-After`` so a misconfigured upstream
# can't park a request indefinitely. 30 s comfortably covers a deploy
# rotation while keeping interactive callers responsive.
DEFAULT_RETRY_AFTER_CAP_S = 30.0

# In-flight 409 polling uses a separate, more generous budget. Long OCR
# / extract jobs run for multiple minutes, so the standard 4 × 30 s cap
# isn't enough to outlast them. With 6 attempts and a 60 s per-attempt
# cap we'll poll for up to ~5 minutes — comfortably below the server's
# 300 s ``STALE_IN_FLIGHT_S`` reclaim, so we won't race the reclaim
# while still giving real jobs time to finish and replay.
DEFAULT_IN_FLIGHT_MAX_ATTEMPTS = 6
DEFAULT_IN_FLIGHT_RETRY_AFTER_CAP_S = 60.0


def is_in_flight_conflict(response: httpx.Response) -> bool:
    """Return ``True`` when a 409 self-identifies as "already in flight".

    The aio-server returns HTTP 409 with a ``Retry-After`` header (and,
    in newer versions, an ``{"error": "already_in_flight", ...}`` JSON
    body) when an identical request is still being processed. Plain
    client 409s — e.g. an idempotency-key fingerprint mismatch — carry
    no ``Retry-After`` and must fail fast.

    We gate on either signal so the SDK works against both the current
    server (header-only) and any future variant that drops the header
    in favour of a structured body. Future *non*-in-flight 409 emitters
    on the server would simply omit both signals and continue to fail
    fast here.
    """
    if response.status_code != 409:
        return False
    if "Retry-After" in response.headers:
        return True
    try:
        body = json.loads(response.content or b"null")
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(body, dict) and body.get("error") == "already_in_flight"


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
    in_flight_max_attempts: int = DEFAULT_IN_FLIGHT_MAX_ATTEMPTS,
    in_flight_retry_after_cap_s: float = DEFAULT_IN_FLIGHT_RETRY_AFTER_CAP_S,
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
        Hard upper bound on total attempts for transient failures
        (5xx / 408 / 425 / 429 / transport errors). Must be ≥ 1.
    base_delay_s, max_delay_s:
        Exponential-backoff parameters used when the server does not
        send a usable ``Retry-After`` header.
    retry_after_cap_s:
        Maximum sleep we'll accept from a server-sent ``Retry-After``
        on a transient-failure retry. Protects callers from a
        misbehaving upstream that asks for impractical wait times.
    in_flight_max_attempts:
        Separate, more generous attempt budget for the in-flight 409
        polling branch. Long OCR / extract jobs can run several minutes,
        so we poll longer here than for plain transient failures.
    in_flight_retry_after_cap_s:
        Per-attempt sleep cap when polling an in-flight 409. Higher than
        the transient cap so honoring the server's ``Retry-After``
        actually waits long enough for real jobs to finish.
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
    if in_flight_max_attempts < 1:
        raise ValueError("in_flight_max_attempts must be >= 1")

    response: httpx.Response | None = None
    transient_attempt = 0
    in_flight_attempt = 0

    # Hard upper bound on the loop so a pathological mix of transient
    # failures and in-flight 409s can't sit here forever. With defaults
    # this is 4 + 6 + safety = 11, well above the maximum we could
    # legitimately need.
    max_total_iterations = max_attempts + in_flight_max_attempts + 2

    for _ in range(max_total_iterations):
        try:
            response = await send()
        except httpx.RequestError:
            # Connect/read timeout, DNS failure, connection reset, etc.
            # Transport-error budget is the transient one; if we've
            # exhausted it, let the caller see the original exception
            # (they translate it into ServiceUnavailableError).
            transient_attempt += 1
            if transient_attempt >= max_attempts:
                raise
            await sleep(
                _jittered_backoff(transient_attempt - 1, base_delay_s, max_delay_s)
            )
            continue

        if response.status_code in RETRY_STATUS:
            transient_attempt += 1
            if transient_attempt >= max_attempts:
                return response
            delay = _delay_from_response(
                response,
                attempt=transient_attempt - 1,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
                retry_after_cap_s=retry_after_cap_s,
            )
            await sleep(delay)
            continue

        if is_in_flight_conflict(response):
            in_flight_attempt += 1
            if in_flight_attempt >= in_flight_max_attempts:
                return response
            delay = _delay_from_response(
                response,
                attempt=in_flight_attempt - 1,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
                retry_after_cap_s=in_flight_retry_after_cap_s,
            )
            await sleep(delay)
            continue

        return response

    # Unreachable in practice: every branch above either returns, sleeps
    # and continues, or raises. Asserted to silence type-checkers and
    # surface logic regressions loudly if the loop is ever refactored.
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
    "DEFAULT_IN_FLIGHT_MAX_ATTEMPTS",
    "DEFAULT_IN_FLIGHT_RETRY_AFTER_CAP_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY_S",
    "DEFAULT_RETRY_AFTER_CAP_S",
    "RETRY_STATUS",
    "is_in_flight_conflict",
    "request_with_retries",
]
