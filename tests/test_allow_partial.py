"""Unit tests for allow_partial / PartialExtraction (no network)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import BaseModel, Field

from clichefactory._extract_validation import RawExtractionValidationError
from clichefactory._extract_finalize import finalize_extract_result
from clichefactory.types import PartialExtraction


class _M(BaseModel):
    x: str = Field(min_length=1)


def test_finalize_returns_partial_when_status_partial() -> None:
    out = finalize_extract_result(
        {"x": None},
        _M,
        None,
        allow_partial=True,
        validation_errors=[{"type": "string_type", "loc": ("x",), "msg": "..."}],
        response_status="partial",
    )
    assert isinstance(out, PartialExtraction)
    assert out.raw["x"] is None
    assert len(out.validation_errors) >= 1


def test_finalize_validates_when_strict() -> None:
    out = finalize_extract_result(
        {"x": "ok"},
        _M,
        None,
        allow_partial=False,
        validation_errors=None,
        response_status="success",
    )
    assert isinstance(out, _M)
    assert out.x == "ok"


def test_local_extract_partial_on_raw_validation_error() -> None:
    from clichefactory._local import extract_local

    errs = [{"type": "string_type", "loc": ("x",), "msg": "none"}]

    class FakeClient:
        def extract(
            self,
            text: str,
            schema: type[BaseModel],
            prompt: str | None = None,
            *,
            raise_on_validation_error: bool = True,
        ):
            if not raise_on_validation_error:
                raise RawExtractionValidationError(data={"x": None}, validation_errors=errs)
            return schema.model_validate({"x": "ok"})

    async def _run() -> object:
        with patch(
            "clichefactory._engine.ai_clients.create_ai_client",
            return_value=FakeClient(),
        ):
            return await extract_local(
                schema=_M,
                file=None,
                text="doc",
                filename=None,
                file_type=None,
                mode=None,
                parsing=None,
                llm=MagicMock(),
                ocr_llm=None,
                include_doc=False,
                include_costs=False,
                postprocess=None,
                allow_partial=True,
            )

    out = asyncio.run(_run())
    assert isinstance(out, PartialExtraction)
    assert out.raw.get("x") is None


def test_service_extract_partial_via_cliche() -> None:
    from clichefactory import factory
    from clichefactory.cliche import Cliche

    errs = [{"type": "string_type", "loc": ("x",), "msg": "none"}]

    async def fake_service(**kwargs: object) -> dict:
        return {
            "status": "partial",
            "result": {"x": None},
            "validation_errors": errs,
            "metadata": {},
        }

    client = factory(
        mode="service",
        api_key="k",
        base_url="http://127.0.0.1:9",
        model=MagicMock(),
        ocr_model=MagicMock(),
    )
    c = Cliche(client=client, schema=_M, name=None, parsing=None, postprocess=None)

    uploaded = MagicMock(file_uri="s3://bucket/uploads/doc.pdf", document_id="doc-1")

    async def _run() -> object:
        with patch(
            "clichefactory._upload.presign_and_upload_file",
            new_callable=AsyncMock,
            return_value=uploaded,
        ), patch(
            "clichefactory._service.service_extract_via_canonical",
            new_callable=AsyncMock,
            side_effect=fake_service,
        ):
            return await c.extract_async(file="/tmp/doc.pdf", allow_partial=True)

    out = asyncio.run(_run())
    assert isinstance(out, PartialExtraction)
    assert out.validation_errors == errs
