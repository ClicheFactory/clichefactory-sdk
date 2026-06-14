from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from clichefactory.errors import (
    AuthenticationError,
    ConfigurationError,
    ErrorInfo,
    ValidationError,
)
from clichefactory.types import (
    Endpoint,
    ParsingOptions,
    PostprocessFn,
    ResolverSpec,
)

T = TypeVar("T", bound=BaseModel)


ClientMode = Literal["service", "local"]


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
                    code="client.ambiguous_model_config",
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


def factory(
    api_key: str | None = None,
    *,
    base_url: str | None = None,
    mode: ClientMode = "service",
    project: str | None = None,
    task: str | None = None,
    model: Endpoint | None = None,
    ocr_model: Endpoint | None = None,
    llm: Endpoint | None = None,
    ocr_llm: Endpoint | None = None,
    parsing: ParsingOptions | None = None,
) -> "Client":
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
    return Client(
        api_key=api_key,
        base_url=base_url,
        mode=mode,
        project=project,
        task=task,
        llm=resolved_model,
        ocr_llm=resolved_ocr_model,
        parsing=parsing,
    )


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None


class Client:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        mode: ClientMode,
        project: str | None = None,
        task: str | None = None,
        llm: Endpoint | None,
        ocr_llm: Endpoint | None,
        parsing: ParsingOptions | None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._mode: ClientMode = mode
        self._project = project
        self._task = task
        self._llm = llm
        self._ocr_llm = ocr_llm
        self._parsing = parsing
        self._scope = Scope()

        if mode not in ("service", "local"):
            raise ConfigurationError(
                ErrorInfo(
                    code="client.invalid_mode",
                    message=f"Invalid mode: {mode!r}",
                    hint="Use mode='service' or mode='local'.",
                )
            )

    @property
    def mode(self) -> ClientMode:
        return self._mode

    @property
    def base_url(self) -> str | None:
        return self._base_url

    def require_service_auth(self) -> str:
        if self._mode != "service":
            raise ConfigurationError(
                ErrorInfo(
                    code="client.service_required",
                    message="This operation requires mode='service'.",
                )
            )
        if not self._api_key:
            raise AuthenticationError(
                ErrorInfo(
                    code="auth.missing_api_key",
                    message="Missing ClicheFactory API key.",
                    hint="Pass api_key=... to factory() (required for service mode calls).",
                )
            )
        return self._api_key

    def with_scope(
        self,
        *,
        tenant_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> "Client":
        warnings.warn(
            "with_scope() is deprecated. Pass project= and task= to factory(), "
            "or use artifact_id= on cliche() for trained extraction.",
            DeprecationWarning,
            stacklevel=2,
        )
        c = Client(
            api_key=self._api_key,
            base_url=self._base_url,
            mode=self._mode,
            project=self._project,
            task=self._task,
            llm=self._llm,
            ocr_llm=self._ocr_llm,
            parsing=self._parsing,
        )
        c._scope = Scope(
            tenant_id=tenant_id if tenant_id is not None else self._scope.tenant_id,
            project_id=project_id or self._scope.project_id,
            task_id=task_id or self._scope.task_id,
        )
        return c

    def cliche(
        self,
        schema: type[T] | dict[str, Any] | None = None,
        *,
        name: str | None = None,
        parsing: ParsingOptions | None = None,
        artifact_id: str | None = None,
        config_id: str | None = None,
        postprocess: PostprocessFn | None = None,
        resolvers: ResolverSpec | None = None,
    ) -> "clichefactory.cliche.Cliche[T]":
        """Create a :class:`~clichefactory.Cliche` bound to this client.

        Parameters
        ----------
        schema:
            Pydantic model class (or canonical schema dict) describing the
            fields to extract.  Optional: omit it to extract **schemaless**
            against a saved config (``config_id=``), in which case
            :meth:`~clichefactory.Cliche.extract` returns the raw result
            ``dict`` (the platform applies the config's schema).
        name:
            Optional label for the cliche (used in logging / Emio UI).
        parsing:
            Override parsing options for this cliche only.
        artifact_id:
            Trained pipeline artifact ID returned by Emio.  When set, the
            service uses the trained extractor instead of the generic LLM.
        config_id:
            Saved config ID (``cfg-...``) published from Emio.  The platform
            resolves its schema, mode, trained artifact, and BYOK model/key
            (service mode only).  Any inline ``schema``/``mode``/``model`` you
            also pass takes precedence over the config's values.  Can also be
            given per call as ``extract(config_id=...)``.
        postprocess:
            Optional callable applied to the extraction result **after** the
            built-in numeric coercion and **before** Pydantic validation.

            Processing pipeline::

                raw LLM dict
                → system coerce  (EU decimals, % suffix, currency, accounting negatives)
                → postprocess    (your hook, if provided)
                → Pydantic model_validate

            The function must accept and return ``dict[str, Any]``.  Use it
            for domain-specific cleanup that the built-in coercion does not
            cover — e.g. normalising date strings, resolving abbreviations, or
            merging split fields.

            Example::

                def fix_dates(result: dict) -> dict:
                    if isinstance(result.get("invoice_date"), str):
                        result["invoice_date"] = parse_date(result["invoice_date"])
                    return result

                cliche = client.cliche(Invoice, postprocess=fix_dates)
        resolvers:
            Default per-field resolvers for
            :meth:`~clichefactory.Cliche.extract_long`.  Keys are top-level
            schema field names, values are callables from
            :mod:`clichefactory.resolvers` or string aliases
            (``"first_non_null"``, ``"concat"``, ``"concat_dedupe_by=<attr>"``,
            etc.).  Resolvers passed to ``extract_long(resolvers=...)`` merge
            over these.  Has no effect on :meth:`~clichefactory.Cliche.extract`.
        """
        from clichefactory.cliche import Cliche

        return Cliche(
            client=self,
            schema=schema,
            name=name,
            parsing=parsing,
            artifact_id=artifact_id,
            config_id=config_id,
            postprocess=postprocess,
            resolvers=resolvers,
        )

    async def to_markdown_async(
        self,
        file: str | bytes,
        *,
        filename: str | None = None,
        file_type: str | None = None,
        parsing: ParsingOptions | None = None,
        conversion_mode: str | None = None,
        include_costs: bool = False,
    ) -> Any:
        if self._mode == "local":
            from clichefactory._local import to_markdown_local

            return await to_markdown_local(
                file=file,
                filename=filename,
                parsing=parsing or self._parsing,
                llm=self._llm,
                ocr_llm=self._ocr_llm,
                include_costs=include_costs,
            )

        # Service path: POST /v1/ocr/to-markdown
        from clichefactory._service import service_to_markdown

        api_key = self.require_service_auth()
        scope = self._scope
        project_id = scope.project_id or self._project or "default"
        task_id = scope.task_id or self._task or "default"
        tenant_id = scope.tenant_id or "default"

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

        if isinstance(file, bytes):
            fname = filename or "document"
            result = await presign_and_upload_bytes(
                base_url=self._base_url,
                api_key=api_key,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                upload_kind="document",
                filename=fname,
                data=file,
            )
        else:
            result = await presign_and_upload_file(
                base_url=self._base_url,
                api_key=api_key,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                upload_kind="document",
                file_path=file,
            )
            fname = filename or result.file_uri.rsplit("/", 1)[-1]
        file_uri = result.file_uri
        file_name = fname

        return await service_to_markdown(
            base_url=self._base_url,
            api_key=api_key,
            tenant_id=tenant_id,
            file_uri=file_uri,
            file_name=file_name,
            mode=conversion_mode if conversion_mode in ("fast", "default") else None,
            ocr_llm=self._ocr_llm,
            parsing=parsing or self._parsing,
        )

    def to_markdown(self, *args: Any, **kwargs: Any) -> Any:
        from clichefactory._utils import run_sync

        return run_sync(self.to_markdown_async(*args, **kwargs))

    async def to_markdown_batch_async(
        self,
        files: list[str | bytes],
        *,
        max_concurrency: int = 5,
        **kwargs: Any,
    ) -> list[Any]:
        """Convert multiple files to markdown concurrently.

        Fans out to to_markdown_async() with a concurrency semaphore.
        All kwargs are forwarded to to_markdown_async().
        """
        sem = asyncio.Semaphore(max_concurrency)

        async def _one(f: str | bytes) -> Any:
            async with sem:
                return await self.to_markdown_async(f, **kwargs)

        return list(await asyncio.gather(*[_one(f) for f in files]))

    def to_markdown_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
        from clichefactory._utils import run_sync

        return run_sync(self.to_markdown_batch_async(*args, **kwargs))

