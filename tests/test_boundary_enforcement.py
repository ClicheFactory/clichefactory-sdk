"""Tests for SDK API boundary enforcement (Plan B).

Validates that:
- S3 URIs are rejected in user-facing extract and to_markdown paths
- `parser` is not sent in service extract payloads
- `ParsingOptions` emits a warning in service extract mode
"""
from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from clichefactory import ValidationError, factory
from clichefactory.errors import ErrorInfo
from clichefactory._service import _extract_config, _parsing_to_payload
from clichefactory.types import ParsingOptions


class SimpleSchema(BaseModel):
    name: str | None = None


# ---------------------------------------------------------------------------
# S3 URI rejection
# ---------------------------------------------------------------------------

class TestS3UriRejection:
    """S3 URIs must be rejected in both extract and to_markdown user paths."""

    def test_extract_rejects_s3_uri(self):
        client = factory(api_key="cliche-test", mode="service")
        c = client.cliche(SimpleSchema)

        with pytest.raises(ValidationError) as exc_info:
            c.extract(file="s3://bucket/key/document.pdf")

        assert exc_info.value.info.code == "input.s3_uri_not_allowed"

    def test_to_markdown_rejects_s3_uri(self):
        client = factory(api_key="cliche-test", mode="service")

        with pytest.raises(ValidationError) as exc_info:
            client.to_markdown(file="s3://bucket/key/document.pdf", mode="service")

        assert exc_info.value.info.code == "input.s3_uri_not_allowed"

    def test_extract_rejects_various_s3_patterns(self):
        client = factory(api_key="cliche-test", mode="service")
        c = client.cliche(SimpleSchema)

        for uri in [
            "s3://bucket/key",
            "s3://my-bucket/path/to/file.pdf",
            "s3://bucket/a",
        ]:
            with pytest.raises(ValidationError) as exc_info:
                c.extract(file=uri)
            assert exc_info.value.info.code == "input.s3_uri_not_allowed"


# ---------------------------------------------------------------------------
# Parser not sent in service extract config
# ---------------------------------------------------------------------------

class TestExtractConfigNoParser:
    """`_extract_config` should not accept or emit a `parser` field."""

    def test_extract_config_has_no_parser_param(self):
        import inspect
        sig = inspect.signature(_extract_config)
        assert "parser" not in sig.parameters

    def test_extract_config_output_has_no_parser(self):
        cfg = _extract_config(mode="fast", llm=None, ocr_llm=None)
        assert "parser" not in cfg
        assert cfg["extraction_mode"] == "fast"

    def test_extract_config_empty(self):
        cfg = _extract_config(mode=None, llm=None, ocr_llm=None)
        assert "parser" not in cfg


# ---------------------------------------------------------------------------
# ParsingOptions and parser warnings in service extract
# ---------------------------------------------------------------------------

class TestServiceExtractWarnings:
    """Service-mode extract should warn when parser or ParsingOptions are provided."""

    @patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
    @patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
    def test_parser_warns_in_service_extract(self, mock_upload, mock_svc):
        mock_upload.return_value = type("R", (), {
            "file_uri": "s3://bucket/uploaded.pdf",
            "document_id": "doc-1",
        })()
        mock_svc.return_value = {"result": {"name": "test"}, "status": "ok"}

        client = factory(api_key="cliche-test", mode="service")
        c = client.cliche(SimpleSchema)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            c.extract(file="/tmp/test.pdf", parser="fast")

        parser_warnings = [x for x in w if "parser" in str(x.message).lower()]
        assert len(parser_warnings) >= 1
        assert "no effect" in str(parser_warnings[0].message).lower()

    @patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
    @patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
    def test_parsing_options_warns_in_service_extract(self, mock_upload, mock_svc):
        mock_upload.return_value = type("R", (), {
            "file_uri": "s3://bucket/uploaded.pdf",
            "document_id": "doc-1",
        })()
        mock_svc.return_value = {"result": {"name": "test"}, "status": "ok"}

        parsing = ParsingOptions(pdf_image_parser="docling")
        client = factory(api_key="cliche-test", mode="service", parsing=parsing)
        c = client.cliche(SimpleSchema)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            c.extract(file="/tmp/test.pdf")

        parsing_warnings = [x for x in w if "ParsingOptions" in str(x.message)]
        assert len(parsing_warnings) >= 1
        assert "local mode" in str(parsing_warnings[0].message).lower()

    @patch("clichefactory._service.service_extract_via_canonical", new_callable=AsyncMock)
    @patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
    def test_no_warning_without_parser_or_parsing(self, mock_upload, mock_svc):
        mock_upload.return_value = type("R", (), {
            "file_uri": "s3://bucket/uploaded.pdf",
            "document_id": "doc-1",
        })()
        mock_svc.return_value = {"result": {"name": "test"}, "status": "ok"}

        client = factory(api_key="cliche-test", mode="service")
        c = client.cliche(SimpleSchema)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            c.extract(file="/tmp/test.pdf")

        boundary_warnings = [
            x for x in w
            if "parser" in str(x.message).lower() or "ParsingOptions" in str(x.message)
        ]
        assert len(boundary_warnings) == 0


# ---------------------------------------------------------------------------
# Parser selection stripped from service to_markdown payload
# ---------------------------------------------------------------------------

class TestServiceToMarkdownNoParserSelection:
    """Service to_markdown auto-routes like extraction: the parser-selection
    fields are not sent, but the orthogonal knobs still are."""

    def test_parser_selection_fields_dropped(self):
        payload = _parsing_to_payload(
            ParsingOptions(
                pdf_image_parser="docling",
                image_parser="docling",
                pdf_fallback_to_ocr_llm=False,
                pdf_ocr_engine="rapidocr",
                pdf_ocr_lang="deu",
            )
        )
        assert payload is not None
        assert "pdf_image_parser" not in payload
        assert "image_parser" not in payload
        # Orthogonal knobs survive.
        assert payload["pdf_fallback_to_ocr_llm"] is False
        assert payload["pdf_ocr_engine"] == "rapidocr"
        assert payload["pdf_ocr_lang"] == "deu"

    def test_parser_only_options_collapse_to_none(self):
        # If the caller set nothing but parser selection, nothing is sent.
        assert _parsing_to_payload(ParsingOptions(pdf_image_parser="docling")) is None
        assert _parsing_to_payload(ParsingOptions(image_parser="docling")) is None

    def test_none_stays_none(self):
        assert _parsing_to_payload(None) is None


class TestServiceToMarkdownWarns:
    """Service to_markdown warns when the caller tries to pick the parser."""

    @patch("clichefactory._service.service_to_markdown", new_callable=AsyncMock)
    @patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
    def test_parser_selection_warns(self, mock_upload, mock_svc):
        mock_upload.return_value = type("R", (), {
            "file_uri": "s3://bucket/uploaded.pdf",
            "document_id": "doc-1",
        })()
        mock_svc.return_value = {"markdown": "# x", "plain_text": "x", "meta": {}}

        parsing = ParsingOptions(pdf_image_parser="docling")
        client = factory(api_key="cliche-test", mode="service", parsing=parsing)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            client.to_markdown(file="/tmp/test.pdf")

        parser_warnings = [
            x for x in w if "pdf_image_parser" in str(x.message)
        ]
        assert len(parser_warnings) >= 1
        assert "service mode" in str(parser_warnings[0].message).lower()

    @patch("clichefactory._service.service_to_markdown", new_callable=AsyncMock)
    @patch("clichefactory._upload.presign_and_upload_file", new_callable=AsyncMock)
    def test_no_warning_for_orthogonal_options(self, mock_upload, mock_svc):
        mock_upload.return_value = type("R", (), {
            "file_uri": "s3://bucket/uploaded.pdf",
            "document_id": "doc-1",
        })()
        mock_svc.return_value = {"markdown": "# x", "plain_text": "x", "meta": {}}

        parsing = ParsingOptions(pdf_ocr_lang="deu")
        client = factory(api_key="cliche-test", mode="service", parsing=parsing)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            client.to_markdown(file="/tmp/test.pdf")

        parser_warnings = [
            x for x in w if "pdf_image_parser" in str(x.message)
        ]
        assert len(parser_warnings) == 0
