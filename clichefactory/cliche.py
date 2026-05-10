from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from clichefactory._utils import run_sync
from clichefactory.errors import (
    ConfigurationError,
    ErrorInfo,
    ValidationError,
)
from clichefactory.types import (
    Endpoint,
    ParsingOptions,
    PostprocessFn,
    Resolver,
    ResolverSpec,
)

T = TypeVar("T", bound=BaseModel)

ExtractionMode = Literal["fast", "trained", "robust", "robust-trained", "one-shot", "two-step", "three-step"]


@dataclass(frozen=True, slots=True)
class ExtractOptions:
    mode: ExtractionMode | None = None
    parsing: ParsingOptions | None = None
    model: Endpoint | None = None
    ocr_model: Endpoint | None = None
    llm: Endpoint | None = None
    ocr_llm: Endpoint | None = None
    include_doc: bool = False
    include_costs: bool = False


def _resolve_endpoint(
    *,
    current: Endpoint | None,
    legacy: Endpoint | None,
    current_name: str,
    legacy_name: str,
) -> Endpoint | None:
    if current is not None and legacy is not None:
        if current.model_dump(mode="json", exclude_none=True) != legacy.model_dump(
            mode="json", exclude_none=True
        ):
            raise ConfigurationError(
                ErrorInfo(
                    code="extract.ambiguous_model_config",
                    message=(
                        f"Both `{current_name}` and legacy `{legacy_name}` were provided with different values."
                    ),
                    hint=f"Use only `{current_name}`.",
                )
            )
        return current
    if current is not None:
        return current
    if legacy is not None:
        warnings.warn(
            f"`{legacy_name}` is deprecated; use `{current_name}` instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        return legacy
    return None


class Cliche(Generic[T]):
    def __init__(
        self,
        *,
        client: "clichefactory.client.Client",
        schema: type[T] | dict[str, Any],
        name: str | None,
        parsing: ParsingOptions | None,
        artifact_id: str | None = None,
        postprocess: PostprocessFn | None = None,
        resolvers: ResolverSpec | None = None,
    ) -> None:
        self._client = client
        self._schema = schema
        self._name = name
        self._parsing = parsing
        self._artifact_id = artifact_id
        self._postprocess = postprocess
        self._resolvers: ResolverSpec | None = dict(resolvers) if resolvers else None

    async def extract_async(
        self,
        *,
        file: str | bytes | None = None,
        text: str | None = None,
        filename: str | None = None,
        file_type: str | None = None,
        mode: ExtractionMode | None = None,
        parsing: ParsingOptions | None = None,
        model: Endpoint | None = None,
        ocr_model: Endpoint | None = None,
        llm: Endpoint | None = None,
        ocr_llm: Endpoint | None = None,
        include_doc: bool = False,
        include_costs: bool = False,
        artifact_id: str | None = None,
        allow_partial: bool = False,
    ) -> Any:  # T | PartialExtraction | tuple
        resolved_model = _resolve_endpoint(
            current=model,
            legacy=llm,
            current_name="model",
            legacy_name="llm",
        )
        resolved_ocr_model = _resolve_endpoint(
            current=ocr_model,
            legacy=ocr_llm,
            current_name="ocr_model",
            legacy_name="ocr_llm",
        )
        if file is None and text is None:
            raise ConfigurationError(
                ErrorInfo(
                    code="extract.missing_input",
                    message="Provide either file=... or text=... to extract().",
                )
            )
        if file is not None and text is not None:
            raise ConfigurationError(
                ErrorInfo(
                    code="extract.ambiguous_input",
                    message="Provide only one of file=... or text=..., not both.",
                )
            )
        # Local path (A2)
        if self._client.mode == "local":
            from clichefactory._local import extract_local

            return await extract_local(
                schema=self._schema,
                file=file,
                text=text,
                filename=filename,
                file_type=file_type,
                mode=mode,
                parsing=parsing or self._parsing or self._client._parsing,  # type: ignore[attr-defined]
                llm=resolved_model or self._client._llm,  # type: ignore[attr-defined]
                ocr_llm=resolved_ocr_model or self._client._ocr_llm,  # type: ignore[attr-defined]
                include_doc=include_doc,
                include_costs=include_costs,
                postprocess=self._postprocess,
                allow_partial=allow_partial,
            )

        # Service path (A3)
        # text= is supported as BYOK one-shot even when client is in service mode.
        if text is not None:
            from clichefactory._local import extract_local

            return await extract_local(
                schema=self._schema,
                file=None,
                text=text,
                filename=filename,
                file_type=file_type,
                mode="one-shot",
                parsing=parsing or self._parsing or self._client._parsing,  # type: ignore[attr-defined]
                llm=resolved_model or self._client._llm,  # type: ignore[attr-defined]
                ocr_llm=resolved_ocr_model or self._client._ocr_llm,  # type: ignore[attr-defined]
                include_doc=include_doc,
                include_costs=include_costs,
                postprocess=self._postprocess,
                allow_partial=allow_partial,
            )

        from clichefactory._service import service_extract_via_canonical
        from clichefactory._extract_finalize import finalize_extract_result

        effective_parsing = parsing or self._parsing or self._client._parsing  # type: ignore[attr-defined]
        if effective_parsing is not None:
            warnings.warn(
                "ParsingOptions is only applied in local mode for extraction. "
                "In service mode, the platform selects the optimal parsing strategy.",
                UserWarning,
                stacklevel=2,
            )

        effective_artifact_id = artifact_id or self._artifact_id
        api_key = self._client.require_service_auth()
        base_url = self._client.base_url
        scope = getattr(self._client, "_scope", None)  # type: ignore[attr-defined]

        if effective_artifact_id:
            tenant_id = "default"
            project_id = "default"
            task_id = "default"
        else:
            project_id = getattr(scope, "project_id", None) or getattr(self._client, "_project", None) or "default"
            task_id = getattr(scope, "task_id", None) or getattr(self._client, "_task", None) or "default"
            tenant_id = getattr(scope, "tenant_id", None) or "default"

        if isinstance(file, str) and file.startswith("s3://"):
            raise ValidationError(
                ErrorInfo(
                    code="input.s3_uri_not_allowed",
                    message="Direct S3 URI input is not supported. Pass a local file path instead.",
                )
            )

        from clichefactory._upload import (
            presign_and_upload_bytes,
            presign_and_upload_file,
        )

        presign_document_id: str | None = None
        if isinstance(file, bytes):
            fname = filename or "document"
            result = await presign_and_upload_bytes(
                base_url=base_url,
                api_key=api_key,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                upload_kind="document",
                filename=fname,
                data=file,
                artifact_id=effective_artifact_id,
            )
        else:
            result = await presign_and_upload_file(
                base_url=base_url,
                api_key=api_key,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                upload_kind="document",
                file_path=file,
                artifact_id=effective_artifact_id,
            )
            fname = filename or result.file_uri.rsplit("/", 1)[-1]
        file_uri = result.file_uri
        file_name = fname
        presign_document_id = result.document_id

        resp = await service_extract_via_canonical(
            base_url=base_url,
            api_key=api_key,
            file_uri=file_uri,
            file_name=file_name,
            schema=self._schema,
            mode=mode,
            llm=resolved_model or self._client._llm,  # type: ignore[attr-defined]
            ocr_llm=resolved_ocr_model or self._client._ocr_llm,  # type: ignore[attr-defined]
            project_id=project_id,
            task_id=task_id,
            tenant_id=tenant_id,
            artifact_id=effective_artifact_id,
            document_id=presign_document_id,
            allow_partial=allow_partial if allow_partial else None,
        )

        result = resp["result"]
        svc_status = resp.get("status") if isinstance(resp, dict) else None
        val_errs = resp.get("validation_errors") if isinstance(resp, dict) else None

        # Processing pipeline:
        #   raw LLM dict
        #   → system coerce  (EU decimals, % suffix, currency, accounting negatives)
        #   → user postprocess  (if provided via cliche(postprocess=...))
        #   → Pydantic model_validate — or PartialExtraction when allow_partial + partial response
        return finalize_extract_result(
            result,
            self._schema,
            self._postprocess,
            allow_partial=allow_partial,
            validation_errors=val_errs if isinstance(val_errs, list) else None,
            response_status=svc_status if isinstance(svc_status, str) else None,
        )

    def extract(self, **kwargs: Any) -> Any:
        return run_sync(self.extract_async(**kwargs))

    async def extract_batch_async(
        self,
        files: list[str | bytes],
        *,
        max_concurrency: int = 5,
        **kwargs: Any,
    ) -> list[Any]:
        """Extract from multiple files concurrently.

        Fans out to extract_async() with a concurrency semaphore.
        All kwargs are forwarded to extract_async().
        """
        sem = asyncio.Semaphore(max_concurrency)

        async def _one(f: str | bytes) -> Any:
            async with sem:
                return await self.extract_async(file=f, **kwargs)

        return list(await asyncio.gather(*[_one(f) for f in files]))

    def extract_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
        return run_sync(self.extract_batch_async(*args, **kwargs))

    # ── Long-document extraction ──────────────────────────────────────────

    async def extract_long_async(
        self,
        *,
        file: str | bytes,
        filename: str | None = None,
        chunker: Any = None,  # clichefactory.chunking.ChunkStrategy
        resolvers: ResolverSpec | None = None,
        default_resolver: Resolver | None = None,
        max_concurrency: int = 4,
        include_chunk_results: bool = False,
        mode: ExtractionMode | None = None,
        parsing: ParsingOptions | None = None,
        model: Endpoint | None = None,
        ocr_model: Endpoint | None = None,
        include_costs: bool = True,
    ) -> Any:
        """Extract from a long document via chunk + merge.

        Use this when a document is too large to fit in one LLM context
        window (typically > ~20 pages).  The pipeline:

        1. Converts the whole file to markdown once.
        2. Splits the markdown into chunks using ``chunker``
           (default :class:`~clichefactory.chunking.TokenChunker`).
        3. Extracts each chunk as a partial result, in parallel up to
           ``max_concurrency``.
        4. Merges per-chunk values field-by-field using ``resolvers``
           (merged over any set on the ``Cliche`` itself).  Fields without
           an explicit resolver get a sensible default based on their JSON
           schema type — ``concat`` for arrays (with a warning),
           ``first_non_null`` for scalars.
        5. Runs the merged dict through the same
           coerce → postprocess → Pydantic-validate pipeline as
           :meth:`extract`.

        Parameters
        ----------
        file:
            Path or ``bytes``.  ``text=`` is not supported here; chunk
            ``text`` yourself and call :meth:`extract` per chunk if you
            already have markdown.
        chunker:
            A :class:`~clichefactory.chunking.ChunkStrategy`.  Defaults to
            ``TokenChunker()``.
        resolvers:
            ``{field_name: resolver}``.  Merged over any cliche-level
            resolvers.  Values may be callables or string aliases
            (e.g. ``"first_non_null"``, ``"concat"``,
            ``"concat_dedupe_by=line_id"``).
        default_resolver:
            Resolver used for any field not in ``resolvers``.  If unset,
            the per-type default policy applies.
        max_concurrency:
            How many chunks to extract in parallel.  Default 4.
        include_chunk_results:
            When ``True``, return a
            :class:`~clichefactory.types.LongExtractionResult` that exposes
            chunks, per-chunk results, per-field values, and which resolver
            fired for each field.  Default ``False`` returns just the
            validated model, matching :meth:`extract`.
        mode:
            Optional extraction mode passed to each chunk-level
            :meth:`extract_async` call.  ``trained`` /
            ``robust`` / ``robust-trained`` and ``artifact_id`` are not
            supported in this SDK release.

        Returns
        -------
        Either ``T`` (when ``include_chunk_results=False``) or
        :class:`~clichefactory.types.LongExtractionResult[T]`.

        Billing
        -------
        Each chunk is a separate :meth:`extract` call; billing accrues per
        page across all chunks.  ``include_chunk_results=True`` surfaces a
        per-chunk cost breakdown under ``result.cost``.
        """
        from clichefactory._long import extract_long_async as _run

        return await _run(
            self,
            file=file,
            filename=filename,
            chunker=chunker,
            resolvers=resolvers,
            default_resolver=default_resolver,
            max_concurrency=max_concurrency,
            include_chunk_results=include_chunk_results,
            mode=mode,
            parsing=parsing,
            model=model,
            ocr_model=ocr_model,
            include_costs=include_costs,
        )

    def extract_long(self, **kwargs: Any) -> Any:
        return run_sync(self.extract_long_async(**kwargs))

