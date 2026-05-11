from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from clichefactory._retry import is_in_flight_conflict, request_with_retries
from clichefactory._schema import simple_schema_to_canonical
from clichefactory._service_url import resolve_service_base_url

from clichefactory.errors import (
    AlreadyInFlightError,
    AuthenticationError,
    ErrorInfo,
    ExtractionError,
    ParsingError,
    ServiceUnavailableError,
    UploadError,
)
from clichefactory.types import Endpoint, ParsingOptions

T = TypeVar("T", bound=BaseModel)


def _headers(api_key: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    h = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    if idempotency_key is not None:
        # The service's public endpoints (/v1/ocr/to-markdown, /v1/uploads/presign,
        # /v1/extract, /v1/to-markdown) accept Idempotency-Key as a header; the
        # canonical envelope endpoint reads it from the body instead.
        h["Idempotency-Key"] = idempotency_key
    return h


def _idempotency_key_for(payload: dict[str, Any]) -> str:
    """Derive a stable Idempotency-Key from a request body.

    The same body must produce the same key on every retry attempt; that's
    the contract the service's idempotency store keys off
    (``(tenant_id, idempotency_key, endpoint)``). Sorting the JSON keys
    keeps dict-iteration order from leaking into the hash.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _endpoint_to_payload(ep: Endpoint | None) -> dict[str, Any] | None:
    if ep is None:
        return None
    payload = ep.model_dump(mode="json", exclude_none=True)
    # Server expects provider_model naming; pass through as-is.
    return payload or None


def _parsing_to_payload(opts: ParsingOptions | None) -> dict[str, Any] | None:
    if opts is None:
        return None
    d = opts.model_dump(mode="json", exclude_none=True)
    if not d:
        return None
    # Map public name to server/internal name.
    if d.get("pdf_image_parser") == "vision_layout":
        d["pdf_image_parser"] = "yolo_per_partes"
    return d


def _extract_config(
    *,
    mode: str | None,
    llm: Endpoint | None,
    ocr_llm: Endpoint | None,
    allow_partial: bool | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if mode is not None:
        cfg["extraction_mode"] = mode
    if allow_partial is not None:
        cfg["allow_partial"] = allow_partial
    ext = _endpoint_to_payload(llm)
    ocr = _endpoint_to_payload(ocr_llm)
    if ext is not None:
        cfg["extraction"] = ext
    if ocr is not None:
        cfg["ocr"] = ocr
    return cfg


def _schema_to_canonical(schema: type[BaseModel] | dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Pydantic model/schema into the canonical JSON Schema format
    expected by the service.

    Note: supports Pydantic v2 (`model_json_schema()`) and a small Pydantic v1
    fallback for compatibility (`schema_json()` / `schema()`).
    """

    if isinstance(schema, dict):
        return simple_schema_to_canonical(schema)

    # Pydantic v2
    if hasattr(schema, "model_json_schema"):
        return simple_schema_to_canonical(schema.model_json_schema())  # type: ignore[attr-defined]

    # Pydantic v1 fallbacks
    if hasattr(schema, "schema_json"):
        return simple_schema_to_canonical(json.loads(schema.schema_json()))  # type: ignore[attr-defined]

    if hasattr(schema, "schema"):
        return simple_schema_to_canonical(schema.schema())  # type: ignore[attr-defined]

    raise AttributeError(f"Unsupported schema object: {schema!r}")


async def service_extract_via_canonical(
    *,
    base_url: str | None,
    api_key: str,
    file_uri: str,
    file_name: str,
    schema: type[T] | dict[str, Any],
    mode: str | None,
    llm: Endpoint | None,
    ocr_llm: Endpoint | None,
    project_id: str,
    task_id: str | None,
    tenant_id: str | None = None,
    artifact_id: str | None = None,
    document_id: str | None = None,
    allow_partial: bool | None = None,
) -> dict[str, Any]:
    # Canonical ingress: validate & route by operation.
    url = resolve_service_base_url(base_url) + "/v1/canonical"

    if not file_uri.startswith("s3://"):
        raise UploadError(
            ErrorInfo(
                code="storage.invalid_file_uri",
                message="Service extraction requires file_uri to be a canonical S3 URI (s3://bucket/key).",
                hint="Upload via the server upload API (Plan B) and pass the returned file_uri here.",
            )
        )

    model_schema = _schema_to_canonical(schema)

    cfg = _extract_config(
        mode=mode, llm=llm, ocr_llm=ocr_llm, allow_partial=allow_partial
    )

    scope: dict[str, Any] = {
        "project_id": project_id,
    }
    if tenant_id is not None:
        scope["tenant_id"] = tenant_id
    if task_id is not None:
        scope["task_id"] = task_id

    resource: dict[str, Any] = {
        "file_uri": file_uri,
    }
    if artifact_id is not None:
        resource["artifact_id"] = artifact_id
    if document_id is not None:
        resource["document_id"] = document_id

    payload: dict[str, Any] = {
        "model_schema": model_schema,
        "file_name": file_name,
        "file_type": file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "file",
        "config": cfg,
    }

    request_id = str(uuid.uuid4())
    idempotency_basis = {
        "operation": "inference.extract",
        "scope": scope,
        "resource": resource,
        "payload": payload,
    }
    idempotency_key = hashlib.sha256(
        json.dumps(idempotency_basis, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    body = {
        "schema_version": "1.0",
        "operation": "inference.extract",
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "scope": scope,
        "resource": resource,
        "payload": payload,
        "metadata": {},
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Body is built once outside the retry loop, so idempotency_key
            # (in the envelope) is identical on every attempt — that's the
            # contract the service keys off for replay.
            async def _send() -> httpx.Response:
                return await client.post(url, json=body, headers=_headers(api_key))

            resp = await request_with_retries(_send)
    except httpx.RequestError as e:
        raise ServiceUnavailableError(
            ErrorInfo(
                code="service.unavailable",
                message="Could not reach ClicheFactory service.",
                hint=str(e),
            )
        ) from e

    if resp.status_code == 401:
        raise AuthenticationError(
            ErrorInfo(code="auth.invalid_api_key", message="Invalid API key for service.")
        )
    if is_in_flight_conflict(resp):
        # Retry budget exhausted while an identical request was still
        # being processed server-side. Surface a dedicated error so
        # callers can distinguish this from a real extract failure and
        # back off with confidence — the same payload will replay later.
        raise AlreadyInFlightError(
            ErrorInfo(
                code="service.already_in_flight",
                message=(
                    "An identical extract request is still being processed "
                    "by the service."
                ),
                hint=(
                    f"Retry in ~{resp.headers.get('Retry-After', '?')}s with "
                    "the same payload; the server will replay the original "
                    "response."
                ),
                retryable=True,
            )
        )
    if resp.status_code != 200:
        raise ExtractionError(
            ErrorInfo(
                code="service.extract_failed",
                message=f"Service extract failed (HTTP {resp.status_code}).",
                hint=resp.text[:2000],
            )
        )
    data = resp.json()
    if not isinstance(data, dict) or "result" not in data:
        raise ExtractionError(
            ErrorInfo(
                code="service.invalid_response",
                message="Service returned an unexpected response for extract.",
                hint=str(data)[:2000],
            )
        )
    return data


@dataclass(frozen=True, slots=True)
class ServiceDoc:
    """Lightweight document returned by service-mode to_markdown.

    Provides the same get_markdown() / get_plain_text() interface as
    NormalizedDoc so callers can use either interchangeably.
    """

    markdown: str
    plain_text: str
    meta: dict[str, Any]

    def get_markdown(self) -> str:
        return self.markdown

    def get_plain_text(self) -> str:
        return self.plain_text


async def service_to_markdown(
    *,
    base_url: str | None,
    api_key: str,
    tenant_id: str,
    file_uri: str,
    file_name: str | None = None,
    mode: str | None = None,
    ocr_llm: Endpoint | None = None,
    parsing: ParsingOptions | None = None,
) -> ServiceDoc:
    """POST /v1/ocr/to-markdown on the service and return a ServiceDoc."""
    url = resolve_service_base_url(base_url) + "/v1/ocr/to-markdown"

    if not file_uri.startswith("s3://"):
        raise UploadError(
            ErrorInfo(
                code="storage.invalid_file_uri",
                message="Service to_markdown requires file_uri to be a canonical S3 URI (s3://bucket/key).",
                hint="Upload the file to S3 first and pass the returned file_uri here.",
            )
        )

    body: dict[str, Any] = {"tenant_id": tenant_id, "file_uri": file_uri}
    if file_name:
        body["file_name"] = file_name
    if mode:
        body["mode"] = mode

    ocr_payload = _endpoint_to_payload(ocr_llm)
    if ocr_payload:
        body["ocr"] = ocr_payload

    parsing_payload = _parsing_to_payload(parsing)
    if parsing_payload:
        body["parsing"] = parsing_payload

    # Derived once before the retry loop so every attempt sends the same
    # key. The service replays the cached response when it sees a key it
    # has seen complete before, preventing duplicate billing on retry.
    idempotency_key = _idempotency_key_for(body)
    request_headers = _headers(api_key, idempotency_key=idempotency_key)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async def _send() -> httpx.Response:
                return await client.post(url, json=body, headers=request_headers)

            resp = await request_with_retries(_send)
    except httpx.RequestError as e:
        raise ServiceUnavailableError(
            ErrorInfo(
                code="service.unavailable",
                message="Could not reach ClicheFactory service.",
                hint=str(e),
            )
        ) from e

    if resp.status_code == 401:
        raise AuthenticationError(
            ErrorInfo(code="auth.invalid_api_key", message="Invalid API key for service.")
        )
    if resp.status_code == 403:
        raise AuthenticationError(
            ErrorInfo(
                code="auth.tenant_scope",
                message="API key is not allowed for this tenant (check tenant_id matches your key).",
                hint=resp.text[:2000],
            )
        )
    if is_in_flight_conflict(resp):
        # Retry budget exhausted while an identical request was still
        # being processed server-side. Surface a dedicated error so
        # callers can distinguish this from a real parsing failure and
        # back off with confidence — the same payload will replay later.
        raise AlreadyInFlightError(
            ErrorInfo(
                code="service.already_in_flight",
                message=(
                    "An identical to_markdown request is still being "
                    "processed by the service."
                ),
                hint=(
                    f"Retry in ~{resp.headers.get('Retry-After', '?')}s with "
                    "the same payload; the server will replay the original "
                    "response."
                ),
                retryable=True,
            )
        )
    if resp.status_code != 200:
        raise ParsingError(
            ErrorInfo(
                code="service.to_markdown_failed",
                message=f"Service to_markdown failed (HTTP {resp.status_code}).",
                hint=resp.text[:2000],
            )
        )

    data = resp.json()
    return ServiceDoc(
        markdown=data.get("markdown", ""),
        plain_text=data.get("plain_text", ""),
        meta=data.get("meta", {}),
    )

