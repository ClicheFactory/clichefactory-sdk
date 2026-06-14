"""Saved-config (``config_id``) support in extract().

Covers both surfaces requested for this release:

* Shape A — typed: ``client.cliche(Model).extract(file=..., config_id=...)``
  returns a validated ``Model`` (the config only fills gaps; inline schema wins).
* Shape B — schemaless: ``client.cliche().extract(file=..., config_id=...)``
  returns the raw result ``dict`` (the platform applies the config's schema).

Plus the guard rails and the canonical request body (``resource.config_id`` and
the empty ``model_schema`` sentinel).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from clichefactory import factory
from clichefactory.errors import ConfigurationError


class Invoice(BaseModel):
    number: str | None = None


def _upload_result():
    return type("R", (), {"file_uri": "s3://bucket/uploaded.pdf", "document_id": "doc-1"})()


# ---------------------------------------------------------------------------
# Shape A: typed cliche + config_id
# ---------------------------------------------------------------------------

@patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
@patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
def test_typed_extract_with_config_id_passes_both(mock_upload, mock_svc):
    mock_upload.return_value = _upload_result()
    mock_svc.return_value = {"result": {"number": "INV-1"}, "status": "success"}

    client = factory(api_key="cliche-test", mode="service")
    out = client.cliche(Invoice).extract(file="/tmp/x.pdf", config_id="cfg-abc")

    # Returns a validated model (Shape A).
    assert isinstance(out, Invoice)
    assert out.number == "INV-1"
    # Both the inline schema and the config_id reach the service; inline schema
    # precedence is resolved server-side.
    kwargs = mock_svc.call_args.kwargs
    assert kwargs["config_id"] == "cfg-abc"
    assert kwargs["schema"] is Invoice


# ---------------------------------------------------------------------------
# Shape B: schemaless cliche + config_id -> dict
# ---------------------------------------------------------------------------

@patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
@patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
def test_schemaless_extract_with_config_id_returns_dict(mock_upload, mock_svc):
    mock_upload.return_value = _upload_result()
    mock_svc.return_value = {"result": {"number": "INV-9", "total": "10"}, "status": "success"}

    client = factory(api_key="cliche-test", mode="service")
    out = client.cliche().extract(file="/tmp/x.pdf", config_id="cfg-abc")

    # No client-side schema -> the server result dict is returned as-is (no
    # schema to coerce field types toward).
    assert isinstance(out, dict)
    assert out == {"number": "INV-9", "total": "10"}
    kwargs = mock_svc.call_args.kwargs
    assert kwargs["config_id"] == "cfg-abc"
    assert kwargs["schema"] is None


# ---------------------------------------------------------------------------
# config_id binding + precedence
# ---------------------------------------------------------------------------

@patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
@patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
def test_config_id_bound_on_cliche(mock_upload, mock_svc):
    mock_upload.return_value = _upload_result()
    mock_svc.return_value = {"result": {}, "status": "success"}

    client = factory(api_key="cliche-test", mode="service")
    client.cliche(config_id="cfg-bound").extract(file="/tmp/x.pdf")

    assert mock_svc.call_args.kwargs["config_id"] == "cfg-bound"


@patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
@patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
def test_per_call_config_id_overrides_bound(mock_upload, mock_svc):
    mock_upload.return_value = _upload_result()
    mock_svc.return_value = {"result": {}, "status": "success"}

    client = factory(api_key="cliche-test", mode="service")
    client.cliche(config_id="cfg-bound").extract(file="/tmp/x.pdf", config_id="cfg-call")

    assert mock_svc.call_args.kwargs["config_id"] == "cfg-call"


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

def test_schemaless_without_config_or_artifact_raises():
    client = factory(api_key="cliche-test", mode="service")
    with pytest.raises(ConfigurationError) as exc:
        client.cliche().extract(file="/tmp/x.pdf")
    assert exc.value.info.code == "extract.no_schema"


def test_config_id_requires_service_mode():
    client = factory(mode="local")
    with pytest.raises(ConfigurationError) as exc:
        client.cliche(Invoice).extract(file="/tmp/x.pdf", config_id="cfg-abc")
    assert exc.value.info.code == "extract.config_id_requires_service"


def test_config_id_with_text_input_raises():
    client = factory(api_key="cliche-test", mode="service")
    with pytest.raises(ConfigurationError) as exc:
        client.cliche(Invoice).extract(text="some text", config_id="cfg-abc")
    assert exc.value.info.code == "extract.text_requires_schema"


# ---------------------------------------------------------------------------
# Canonical request body: resource.config_id + empty model_schema sentinel
# ---------------------------------------------------------------------------

def test_service_body_carries_config_id_and_empty_schema():
    from clichefactory import _service

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return {"result": {"number": "1"}, "status": "success"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["body"] = json
            return _FakeResp()

    async def _run():
        with patch.object(_service.httpx, "AsyncClient", _FakeClient):
            return await _service.service_extract_via_canonical(
                base_url="http://127.0.0.1:9",
                api_key="k",
                file_uri="s3://bucket/doc.pdf",
                file_name="doc.pdf",
                schema=None,
                mode=None,
                llm=None,
                ocr_llm=None,
                project_id="default",
                task_id="default",
                tenant_id="default",
                config_id="cfg-xyz",
            )

    asyncio.run(_run())
    body = captured["body"]
    assert body["resource"]["config_id"] == "cfg-xyz"
    # Schemaless sentinel: empty model_schema, server fills from the config.
    assert body["payload"]["model_schema"] == {}
