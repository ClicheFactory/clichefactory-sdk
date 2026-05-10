"""Presigned-URL upload helpers for service mode.

Handles presigning via the ClicheFactory service and uploading file bytes via HTTP PUT.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from clichefactory._retry import request_with_retries
from clichefactory.errors import (
    ErrorInfo,
    ServiceUnavailableError,
    UploadError,
)
from clichefactory._service_url import resolve_service_base_url

UploadKind = Literal["document", "training_input", "training_ground_truth"]


@dataclass(frozen=True, slots=True)
class PresignResult:
    upload_url: str
    file_uri: str
    method: str
    headers: dict[str, str]
    expires_in_s: int
    dataset_id: str | None
    document_id: str | None


def _headers(api_key: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    h = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    if idempotency_key is not None:
        h["Idempotency-Key"] = idempotency_key
    return h


def _idempotency_key_for(payload: dict[str, Any]) -> str:
    """Derive a stable Idempotency-Key from a presign request body."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _guess_content_type(filename: str) -> str | None:
    ct, _ = mimetypes.guess_type(filename)
    return ct


async def presign(
    *,
    base_url: str | None,
    api_key: str,
    tenant_id: str,
    project_id: str,
    task_id: str,
    upload_kind: UploadKind,
    filename: str,
    content_length: int | None = None,
    content_type: str | None = None,
    dataset_id: str | None = None,
    document_id: str | None = None,
    artifact_id: str | None = None,
) -> PresignResult:
    """Call the service presign endpoint and return the result."""
    url = resolve_service_base_url(base_url) + "/v1/uploads/presign"

    body: dict[str, Any] = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_id": task_id,
        "upload_kind": upload_kind,
        "filename": filename,
    }
    if content_length is not None:
        body["content_length"] = content_length
    if content_type is not None:
        body["content_type"] = content_type
    if dataset_id is not None:
        body["dataset_id"] = dataset_id
    if document_id is not None:
        body["document_id"] = document_id
    if artifact_id is not None:
        body["artifact_id"] = artifact_id

    # Stable across retries so the service can replay the same presign result
    # rather than minting a new file_uri / document_id every time.
    idempotency_key = _idempotency_key_for(body)
    request_headers = _headers(api_key, idempotency_key=idempotency_key)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async def _send() -> httpx.Response:
                return await client.post(url, json=body, headers=request_headers)

            resp = await request_with_retries(_send)
    except httpx.RequestError as e:
        raise ServiceUnavailableError(
            ErrorInfo(
                code="service.unavailable",
                message="Could not reach ClicheFactory service for presign.",
                hint=str(e),
            )
        ) from e

    if resp.status_code != 200:
        raise UploadError(
            ErrorInfo(
                code="upload.presign_failed",
                message=f"Presign failed (HTTP {resp.status_code}).",
                hint=resp.text[:2000],
            )
        )

    data = resp.json()
    return PresignResult(
        upload_url=data["upload_url"],
        file_uri=data["file_uri"],
        method=data.get("method", "PUT"),
        headers=data.get("headers", {}),
        expires_in_s=data.get("expires_in_s", 3600),
        dataset_id=data.get("dataset_id"),
        document_id=data.get("document_id"),
    )


async def upload_bytes(
    *,
    upload_url: str,
    data: bytes,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> None:
    """PUT file bytes to a presigned URL.

    Retried on transient transport errors and 5xx. S3-style PUTs are
    inherently idempotent on the URL+content (the URL embeds the object
    key), so no idempotency key is involved — re-uploading the same
    bytes to the same presigned URL is safe.
    """
    put_headers: dict[str, str] = dict(headers or {})
    if content_type:
        put_headers["Content-Type"] = content_type

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async def _send() -> httpx.Response:
                return await client.put(upload_url, content=data, headers=put_headers)

            resp = await request_with_retries(_send)
    except httpx.RequestError as e:
        raise UploadError(
            ErrorInfo(
                code="upload.put_failed",
                message="Failed to upload file to presigned URL.",
                hint=str(e),
            )
        ) from e

    if resp.status_code not in (200, 201, 204):
        raise UploadError(
            ErrorInfo(
                code="upload.put_rejected",
                message=f"Presigned PUT rejected (HTTP {resp.status_code}).",
                hint=resp.text[:2000],
            )
        )


async def presign_and_upload_file(
    *,
    base_url: str | None,
    api_key: str,
    tenant_id: str,
    project_id: str,
    task_id: str,
    upload_kind: UploadKind,
    file_path: str | Path,
    dataset_id: str | None = None,
    document_id: str | None = None,
    artifact_id: str | None = None,
) -> PresignResult:
    """Presign and upload a single local file. Returns the presign result with file_uri."""
    path = Path(file_path)
    if not path.is_file():
        raise UploadError(
            ErrorInfo(
                code="upload.file_not_found",
                message=f"File not found: {path}",
            )
        )

    data = path.read_bytes()
    filename = path.name
    content_type = _guess_content_type(filename)

    result = await presign(
        base_url=base_url,
        api_key=api_key,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        upload_kind=upload_kind,
        filename=filename,
        content_length=len(data),
        content_type=content_type,
        dataset_id=dataset_id,
        document_id=document_id,
        artifact_id=artifact_id,
    )

    await upload_bytes(
        upload_url=result.upload_url,
        data=data,
        headers=result.headers,
        content_type=content_type,
    )

    return result


async def presign_and_upload_bytes(
    *,
    base_url: str | None,
    api_key: str,
    tenant_id: str,
    project_id: str,
    task_id: str,
    upload_kind: UploadKind,
    filename: str,
    data: bytes,
    dataset_id: str | None = None,
    document_id: str | None = None,
    artifact_id: str | None = None,
) -> PresignResult:
    """Presign and upload raw bytes. Returns the presign result with file_uri."""
    content_type = _guess_content_type(filename)

    result = await presign(
        base_url=base_url,
        api_key=api_key,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        upload_kind=upload_kind,
        filename=filename,
        content_length=len(data),
        content_type=content_type,
        dataset_id=dataset_id,
        document_id=document_id,
        artifact_id=artifact_id,
    )

    await upload_bytes(
        upload_url=result.upload_url,
        data=data,
        headers=result.headers,
        content_type=content_type,
    )

    return result
