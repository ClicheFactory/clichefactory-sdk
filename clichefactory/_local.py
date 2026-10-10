from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, TypeVar

import anyio
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from clichefactory._schema import canonical_schema_to_pydantic
from clichefactory.errors import (
    ConfigurationError,
    ErrorInfo,
    ExtractionError,
    ParsingError,
    UnsupportedModeError,
    UnsupportedParserError,
    ValidationError,
)
from clichefactory.types import Endpoint, ParsingOptions, PostprocessFn

T = TypeVar("T", bound=BaseModel)


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def _default_extraction_model() -> str:
    return (
        _env("MODEL_NAME")
        or _env("CLICHEFACTORY_LLM_MODEL_NAME")
        or _env("EXTRACTION_LLM_MODEL_NAME")
        or _env("LLM_MODEL_NAME")
    )


def _default_extraction_key() -> str:
    return (
        _env("MODEL_API_KEY")
        or _env("CLICHEFACTORY_LLM_API_KEY")
        or _env("EXTRACTION_LLM_API_KEY")
        or _env("LLM_API_KEY")
        or ""
    )


def _default_ocr_model() -> str:
    return (
        _env("OCR_MODEL_NAME")
        or _env("MODEL_NAME")
        or _env("CLICHEFACTORY_OCR_MODEL_NAME")
        or _env("OCR_LLM_MODEL_NAME")
    )


def _default_ocr_key() -> str:
    return (
        _env("OCR_MODEL_API_KEY")
        or _env("MODEL_API_KEY")
        or _env("CLICHEFACTORY_OCR_API_KEY")
        or _env("OCR_LLM_API_KEY")
        or _env("OCR_API_KEY")
        or _default_extraction_key()
    )


def _raise_local_missing_deps(exc: ImportError) -> None:
    raise ConfigurationError(
        ErrorInfo(
            code="local.missing_deps",
            message="Local mode requires additional dependencies.",
            hint="Install with: pip install clichefactory[local]",
        )
    ) from exc


def _model_allows_empty_api_key(provider_model: str) -> bool:
    m = (provider_model or "").strip()
    return bool(m.startswith("ollama/"))


def _local_requires_ocr_llm(parsing: ParsingOptions) -> bool:
    pdf_fb = True if parsing.pdf_fallback_to_ocr_llm is None else parsing.pdf_fallback_to_ocr_llm
    img_fb = True if parsing.image_parser_fallback is None else parsing.image_parser_fallback
    pdf_img = parsing.pdf_image_parser or "docling"
    img = parsing.image_parser or "rapidocr"
    if pdf_img in {"docling_vlm", "ocr_llm"}:
        return True
    if img == "ocr_llm":
        return True
    if pdf_fb:
        return True
    if img_fb:
        return True
    return False


def _validate_local_llm_config(
    *,
    extraction_model: str,
    extraction_key: str,
    ocr_model: str,
    ocr_key: str,
    parsing: ParsingOptions,
) -> None:
    em = extraction_model.strip()
    ek = extraction_key.strip()
    if not em:
        raise ConfigurationError(
            ErrorInfo(
                code="local.missing_llm",
                message="No extraction LLM is configured for local mode.",
                hint=(
                    "Set factory(model=Endpoint(provider_model=..., api_key=...)) "
                    "or environment variables LLM_MODEL_NAME and LLM_API_KEY. "
                    "Optionally set ocr_model=Endpoint(...) or OCR_MODEL_NAME / OCR_MODEL_API_KEY "
                    "when parsing uses OCR LLM fallback or VLM refinement."
                ),
            )
        )
    if not ek and not _model_allows_empty_api_key(em):
        raise ConfigurationError(
            ErrorInfo(
                code="local.missing_llm_key",
                message="Missing API key for the configured extraction LLM.",
                hint=(
                    "Set LLM_API_KEY (or model=Endpoint(..., api_key=...)). "
                    "Ollama models may use an empty api_key when the server does not require one."
                ),
            )
        )
    if not _local_requires_ocr_llm(parsing):
        return
    om = ocr_model.strip()
    ok = ocr_key.strip()
    if not om or (not ok and not _model_allows_empty_api_key(om)):
        raise ConfigurationError(
            ErrorInfo(
                code="local.missing_ocr_llm",
                message="Current parsing options require an OCR LLM, but none is configured.",
                hint=(
                    "Configure OCR (same as extraction is fine if you use one model for both): "
                    "factory(ocr_model=Endpoint(...)) or OCR_MODEL_NAME and OCR_MODEL_API_KEY, "
                    "or set parsing options that use only non-LLM OCR (e.g. "
                    "ParsingOptions(pdf_fallback_to_ocr_llm=False, image_parser_fallback=False))."
                ),
            )
        )


def build_aio_config(
    *,
    llm: Endpoint | None,
    ocr_llm: Endpoint | None,
    parsing: ParsingOptions | None,
    include_costs: bool = False,
) -> tuple[Any, Any | None]:
    try:
        from clichefactory._engine.config import AioConfig
    except ImportError as exc:
        _raise_local_missing_deps(exc)

    parsing = parsing or ParsingOptions()

    if parsing.pdf_image_parser == "vision_layout":
        raise UnsupportedParserError(
            ErrorInfo(
                code="parser.unsupported_local",
                message="pdf_image_parser='vision_layout' is SaaS-only and not available in local mode.",
                hint="Use pdf_image_parser='docling' (default) or run with mode='service'.",
            )
        )

    # Resolve extraction first.
    extraction_model = (llm.provider_model if llm else None) or _default_extraction_model()
    extraction_key = (llm.api_key if llm else None) or _default_extraction_key()
    extraction_api_base = (llm.api_base if llm else None) or ""
    extraction_max_tokens = (llm.max_tokens if llm else None) or 10000
    extraction_temperature = (llm.temperature if llm else None) or 0.1
    extraction_num_retries = (llm.num_retries if llm else None) or 8
    extraction_thinking_level = llm.thinking_level if llm else None

    # OCR: explicit override wins; otherwise inherit from extraction.
    if ocr_llm is not None:
        ocr_model = ocr_llm.provider_model or extraction_model or _default_ocr_model()
        ocr_key = ocr_llm.api_key or extraction_key or _default_ocr_key()
        ocr_api_base = ocr_llm.api_base or extraction_api_base
        ocr_max_tokens = ocr_llm.max_tokens or extraction_max_tokens
        ocr_temperature = ocr_llm.temperature if ocr_llm.temperature is not None else 1.0
        ocr_num_retries = ocr_llm.num_retries or extraction_num_retries
        ocr_thinking_level = ocr_llm.thinking_level or extraction_thinking_level
    else:
        ocr_model = extraction_model or _default_ocr_model()
        ocr_key = extraction_key or _default_ocr_key()
        ocr_api_base = extraction_api_base
        ocr_max_tokens = extraction_max_tokens
        ocr_temperature = 1.0
        ocr_num_retries = extraction_num_retries
        ocr_thinking_level = extraction_thinking_level

    _validate_local_llm_config(
        extraction_model=extraction_model,
        extraction_key=extraction_key,
        ocr_model=ocr_model,
        ocr_key=ocr_key,
        parsing=parsing,
    )

    # Local SDK no longer computes or tracks costs at runtime.
    tracker: Any | None = None

    cfg = AioConfig(
        ocr_llm_model_name=ocr_model,
        ocr_llm_api_key=ocr_key,
        ocr_llm_api_base=ocr_api_base,
        ocr_llm_max_tokens=ocr_max_tokens,
        ocr_llm_temperature=ocr_temperature,
        ocr_llm_num_retries=ocr_num_retries,
        ocr_llm_thinking_level=ocr_thinking_level,
        extraction_llm_model_name=extraction_model,
        extraction_llm_api_key=extraction_key,
        extraction_llm_api_base=extraction_api_base,
        extraction_llm_max_tokens=extraction_max_tokens,
        extraction_llm_temperature=extraction_temperature,
        extraction_llm_num_retries=extraction_num_retries,
        extraction_llm_thinking_level=extraction_thinking_level,
        pdf_image_parser=(parsing.pdf_image_parser or "docling"),  # type: ignore[arg-type]
        pdf_fallback_to_ocr_llm=bool(True if parsing.pdf_fallback_to_ocr_llm is None else parsing.pdf_fallback_to_ocr_llm),
        pdf_structured_fallback_to_image=bool(False if parsing.pdf_structured_fallback_to_image is None else parsing.pdf_structured_fallback_to_image),
        pdf_ocr_engine=(parsing.pdf_ocr_engine or "rapidocr"),  # type: ignore[arg-type]
        pdf_ocr_lang=(parsing.pdf_ocr_lang or "eng"),
        use_ocr_llm_body=bool(True if parsing.use_ocr_llm_body is None else parsing.use_ocr_llm_body),
        image_parser=(parsing.image_parser or "rapidocr"),  # type: ignore[arg-type]
        image_parser_fallback=bool(True if parsing.image_parser_fallback is None else parsing.image_parser_fallback),
        image_parser_lang=(parsing.image_parser_lang or "eng"),
        cost_tracking_enabled=False,
        model_pricing_path=None,
        usage_tracker=None,
        cacher=None,
    )
    return cfg, tracker


def build_default_registry(config: Any) -> Any:
    try:
        from clichefactory._engine.parsers.csv_parser import CsvParser
        from clichefactory._engine.parsers.doc_parser import DocParser
        from clichefactory._engine.parsers.docx_parser import DocxParser
        from clichefactory._engine.parsers.eml_parser import EmlParser
        from clichefactory._engine.parsers.image_parser import ImageRouterParser
        from clichefactory._engine.parsers.media_parser_registry import MediaParserRegistry
        from clichefactory._engine.parsers.pdf_parser import PdfRouterParser
        from clichefactory._engine.parsers.text_parser import TextParser
        from clichefactory._engine.parsers.xlsx_parser import XlsxParser
    except ImportError as exc:
        _raise_local_missing_deps(exc)

    registry = MediaParserRegistry()
    registry.config = config

    registry.register(".pdf", PdfRouterParser)
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        registry.register(ext, ImageRouterParser)
    registry.register(".eml", EmlParser)
    registry.register(".docx", DocxParser)
    registry.register_many([".odt", ".doc"], DocParser)
    registry.register(".xlsx", XlsxParser)
    registry.register(".csv", CsvParser)
    registry.register(".txt", TextParser)
    registry.register(".md", TextParser)
    return registry


def _load_file_bytes(file: str | Path | bytes, *, filename: str | None) -> tuple[bytes, str]:
    if isinstance(file, (str, Path)):
        path = Path(file)
        return path.read_bytes(), (filename or path.name)
    if isinstance(file, (bytes, bytearray)):
        if not filename:
            raise ValidationError(
                ErrorInfo(
                    code="file.missing_filename",
                    message="When passing raw bytes, you must also pass filename=... so the parser can route correctly.",
                )
            )
        return bytes(file), filename
    raise ValidationError(
        ErrorInfo(
            code="file.invalid_type",
            message=f"Unsupported file input type: {type(file)!r}",
        )
    )


async def to_markdown_local(
    *,
    file: str | Path | bytes,
    filename: str | None,
    parsing: ParsingOptions | None,
    llm: Endpoint | None,
    ocr_llm: Endpoint | None,
    include_costs: bool,
) -> Any:
    try:
        from clichefactory._engine.parsers.parser_utils.media_router import MediaRouter
        from clichefactory._engine.parsers.parser_utils.media_type_detector import MediaTypeDetector
    except ImportError as exc:
        _raise_local_missing_deps(exc)

    cfg, _tracker = build_aio_config(llm=llm, ocr_llm=ocr_llm, parsing=parsing, include_costs=include_costs)
    registry = build_default_registry(cfg)
    router = MediaRouter(registry, cacher=None, detector=MediaTypeDetector())
    content, fname = _load_file_bytes(file, filename=filename)

    def _parse_sync():
        doc = router.parse(content, fname)
        if doc is None:
            raise ParsingError(
                ErrorInfo(
                    code="parser.not_found",
                    message=f"No parser registered for filename {fname!r}.",
                )
            )
        return doc

    return await anyio.to_thread.run_sync(_parse_sync)


async def extract_local(
    *,
    schema: type[T] | dict[str, Any],
    file: str | Path | bytes | None,
    text: str | None,
    filename: str | None,
    file_type: str | None,
    mode: str | None,
    parsing: ParsingOptions | None,
    llm: Endpoint | None,
    ocr_llm: Endpoint | None,
    include_doc: bool,
    include_costs: bool,
    postprocess: PostprocessFn | None = None,
    allow_partial: bool = False,
) -> Any:
    try:
        from clichefactory._engine.ai_clients import create_ai_client
        from clichefactory._extract_validation import RawExtractionValidationError
        from clichefactory._engine.parsers.parser_utils.media_type_detector import MediaTypeDetector
        from clichefactory._extract_finalize import finalize_extract_result
    except ImportError as exc:
        _raise_local_missing_deps(exc)

    if mode in ("trained", "robust", "robust-trained", "two-step", "three-step"):
        raise UnsupportedModeError(
            ErrorInfo(
                code="mode.unsupported_local",
                message=f"Extraction mode {mode!r} is SaaS-only and not available in local mode.",
                hint="Use mode=None (OCR->extract) or mode='fast' (file->LLM) locally, or run with mode='service'.",
            )
        )

    schema_cls: type[T]
    if isinstance(schema, dict):
        try:
            schema_cls = canonical_schema_to_pydantic(schema)  # type: ignore[assignment]
        except Exception as e:
            raise ValidationError(
                ErrorInfo(
                    code="schema.invalid",
                    message=f"Invalid schema: {e}",
                )
            ) from e
    else:
        schema_cls = schema

    cfg, tracker = build_aio_config(llm=llm, ocr_llm=ocr_llm, parsing=parsing, include_costs=include_costs)
    client = create_ai_client(cfg, purpose="extraction")

    ro = not allow_partial

    if text is not None:
        try:
            out = client.extract(
                text=text, schema=schema_cls, raise_on_validation_error=ro
            )
        except RawExtractionValidationError as exc:
            if allow_partial:
                return finalize_extract_result(
                    exc.data,
                    schema,
                    postprocess,
                    allow_partial=True,
                    validation_errors=exc.validation_errors,
                    response_status=None,
                )
            raise exc.__cause__ from exc
        except Exception as e:
            raise ExtractionError(
                ErrorInfo(code="extract.failed", message=str(e))
            ) from e
        return out

    assert file is not None
    content, fname = _load_file_bytes(file, filename=filename)
    detected = MediaTypeDetector().detect(content, fname)
    mime = detected.mime or "application/octet-stream"

    # include_doc forces markdown normalization (so user can inspect what was extracted from)
    if include_doc or (mode not in ("fast", "one-shot") and mode is not None):
        doc = await to_markdown_local(
            file=content,
            filename=fname,
            parsing=parsing,
            llm=llm,
            ocr_llm=ocr_llm,
            include_costs=include_costs,
        )
        try:
            out = client.extract(
                text=doc.get_markdown(), schema=schema_cls, raise_on_validation_error=ro
            )
        except RawExtractionValidationError as exc:
            if allow_partial:
                fin = finalize_extract_result(
                    exc.data,
                    schema,
                    postprocess,
                    allow_partial=True,
                    validation_errors=exc.validation_errors,
                    response_status=None,
                )
                return (fin, doc) if include_doc else fin
            raise exc.__cause__ from exc
        except Exception as e:
            raise ExtractionError(ErrorInfo(code="extract.failed", message=str(e))) from e
        return (out, doc) if include_doc else out

    if mode in ("fast", "one-shot"):
        from clichefactory._engine.ai_clients import client_supports_bytes

        if client_supports_bytes(client, mime):
            logger.debug(
                "extract_local: fast path via extract_from_bytes (mime=%s, file=%s)",
                mime,
                fname,
            )
            try:
                out = client.extract_from_bytes(
                    content=content,
                    mime=mime,
                    schema=schema_cls,
                    raise_on_validation_error=ro,
                )
            except RawExtractionValidationError as exc:
                if allow_partial:
                    return finalize_extract_result(
                        exc.data,
                        schema,
                        postprocess,
                        allow_partial=True,
                        validation_errors=exc.validation_errors,
                        response_status=None,
                    )
                raise exc.__cause__ from exc
            except Exception as e:
                raise ExtractionError(ErrorInfo(code="extract.failed", message=str(e))) from e
            return out

        # Vendor does not accept these bytes (EML, DOCX, XLSX, ODT, …).
        # Degrade to: parse → markdown → single extract(text, schema) call.
        # Fast-mode semantics are preserved (one LLM call for extraction, no
        # DSPy pipeline); the parse step adds local CPU work but keeps the
        # call working for every format we have a parser for.
        logger.info(
            "extract_local: fast path degrading to markdown (mime=%s, file=%s); "
            "vendor does not accept these bytes directly",
            mime,
            fname,
        )
        doc = await to_markdown_local(
            file=content,
            filename=fname,
            parsing=parsing,
            llm=llm,
            ocr_llm=ocr_llm,
            include_costs=include_costs,
        )
        try:
            out = client.extract(
                text=doc.get_markdown(),
                schema=schema_cls,
                raise_on_validation_error=ro,
            )
        except RawExtractionValidationError as exc:
            if allow_partial:
                return finalize_extract_result(
                    exc.data,
                    schema,
                    postprocess,
                    allow_partial=True,
                    validation_errors=exc.validation_errors,
                    response_status=None,
                )
            raise exc.__cause__ from exc
        except Exception as e:
            raise ExtractionError(ErrorInfo(code="extract.failed", message=str(e))) from e
        return out

    # Default local path: parse -> markdown -> extract(text)
    doc = await to_markdown_local(
        file=content,
        filename=fname,
        parsing=parsing,
        llm=llm,
        ocr_llm=ocr_llm,
        include_costs=include_costs,
    )
    try:
        out = client.extract(
            text=doc.get_markdown(), schema=schema_cls, raise_on_validation_error=ro
        )
    except RawExtractionValidationError as exc:
        if allow_partial:
            fin = finalize_extract_result(
                exc.data,
                schema,
                postprocess,
                allow_partial=True,
                validation_errors=exc.validation_errors,
                response_status=None,
            )
            return (fin, doc) if include_doc else fin
        raise exc.__cause__ from exc
    except Exception as e:
        raise ExtractionError(ErrorInfo(code="extract.failed", message=str(e))) from e
    return (out, doc) if include_doc else out

