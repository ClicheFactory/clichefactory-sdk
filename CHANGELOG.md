# Changelog

All notable changes to `clichefactory` are documented in this file.

## [0.6.2] — 2026-05-18

### Changed

- Update package repository metadata to point at the public
  `ClicheFactory/clichefactory-sdk` GitHub repository.

## [0.6.1] — 2026-05-14

### Fixed

- **`XlsxParser` and `DocxParser` could not be instantiated through
  `MediaParserRegistry`.** `MediaParserRegistry.create_parser` always
  forwards `media_parser_registry=<self>` to the parser constructor so
  parsers that need to resolve sibling parsers (PDF, EML attachments)
  can do so. `XlsxParser.__init__` and `DocxParser.__init__` did not
  accept that kwarg, so any consumer that built parsers through the
  registry (i.e. the canonical `MediaRouter` path) crashed with
  `TypeError: __init__() got an unexpected keyword argument
  'media_parser_registry'` the moment an `.xlsx` or `.docx` arrived.
  Both constructors now accept `**kwargs` and forward through to
  `MediaParser.__init__`, matching the rest of the parser family
  (`CsvParser`, `TextParser`, `DocParser`, `EmlParser`,
  `PdfRouterParser`). A regression test (`tests/test_media_parser_registry.py`)
  round-trips every parser through `create_parser` so this signature
  drift can't recur silently.

### Changed

- **`.odt` parsing now goes through LibreOffice (`soffice`), not
  pandoc.** `office_converter` previously required pandoc for `.docx`
  and `.odt` and only fell back to `soffice` for legacy `.doc`. In
  practice `.docx` never hit this path (Docling handles it directly via
  `DocxParser`), and pandoc is heavier to install than LibreOffice
  while covering the same legacy office formats less reliably.
  `convert_office_bytes_to_pdf_bytes` / `convert_office_to_pdf_path`
  now route both `.doc` and `.odt` through `soffice --headless`. Pandoc
  remains supported as a best-effort fallback if a host has pandoc but
  no LibreOffice, but it is no longer a hard runtime requirement of
  the default installation.
- `clichefactory doctor` reflects the new contract: `soffice` is the
  required binary for `.doc` / `.odt`, `pandoc` is reported as
  informational only.

## [0.6.0] — 2026-05-11

### Fixed

- **In-flight 409s no longer surface as opaque `ParsingError` /
  `ExtractionError` / `UploadError`.** The service deduplicates requests
  by `(tenant_id, idempotency_key, endpoint)` and returns HTTP 409 with
  a `Retry-After` header while a previous attempt is still being
  processed. The retry layer used to bail immediately on 409 because
  it wasn't in `RETRY_STATUS`, so re-running a script before the
  original OCR / extract job finished (or recovering from a transport
  hiccup that left the server still working) would fail with a generic
  HTTP error and no guidance. The retry helper now detects the
  "already in flight" variant of 409 — either by the `Retry-After`
  header or a structured `{"error": "already_in_flight"}` response body
  — and polls with a separate, more generous attempt budget
  (`DEFAULT_IN_FLIGHT_MAX_ATTEMPTS = 6`, cap `60 s`) so it actually
  outlasts real multi-minute jobs and replays once they complete.
  Plain 409s without these markers (e.g. an idempotency-key
  fingerprint mismatch) still fail fast.

### Added

- `clichefactory.AlreadyInFlightError` — typed error raised when
  polling exhausts the in-flight retry budget but the server is still
  processing an identical request. Carries
  `code="service.already_in_flight"`, `retryable=True`, and a hint
  with the server's last `Retry-After`. Re-running the same call later
  is safe: the SDK derives the idempotency key from the request body,
  so the server will replay the original response.
- `clichefactory._retry.is_in_flight_conflict(response)` helper for
  callsites that need to branch on this specific 409 variant.
- Tunables on `request_with_retries`: `in_flight_max_attempts` and
  `in_flight_retry_after_cap_s` (separate from the transient
  `max_attempts` / `retry_after_cap_s` budget). Internal API.

## [0.5.1] — 2026-05-10

### Fixed

- **`PageChunker` now actually splits multi-page documents.** The
  Docling adapter's `get_markdown()` produced markdown with no page
  markers, so `clichefactory.chunking.PageChunker` always fell back
  to `TokenChunker` and emitted a single chunk for any multi-page
  document under the token cap. `extract_long(..., chunker=PageChunker(
  pages_per_chunk=N))` therefore never exercised its merge path on
  small docs. The adapter now assembles per-page markdown via
  `build_per_page_markdown` and joins it with the canonical
  `<!-- cf:page N -->` markers that `PageChunker._PAGE_MARKER_PATTERNS`
  matches first. Both the default `output_mode="markdown"` path and
  the `output_mode="structured"` path emit markers; the structured
  path uses a new `pages_to_markdown` helper that walks the
  document-model `Page` sequence directly.

### Added

- `emit_page_marker(n)`, `assemble_paged_markdown(pages_md)`, and
  `pages_to_markdown(pages)` helpers in
  `clichefactory._engine.parsers.parser_utils.pdf.docling_helpers`.
  Internal API; surfaced for parsers that build their own markdown
  outside the Docling adapter and want PageChunker compatibility.

## [0.5.0] — 2026-05-10

### Removed

- **`environment` plumbing dropped from the SDK.** The
  `environment=` keyword argument on the deprecated
  `Client.with_scope()`, the `environment` field on the internal
  `Scope` dataclass, and the `"environment"` key on the canonical
  envelope `scope` and on the presign request body are all gone.
  The ClicheFactory backend treats `environment` as request metadata
  only — it has not driven bucket selection or any other routing for
  several releases — and the matching server-side request schemas
  declare it optional with a default, so omitting it is wire-compatible
  with the deployed service. No public caller in this codebase passed
  `environment` through any of these surfaces, so this is effectively
  a dead-code removal; the externally visible behavior is unchanged.

### Documentation

- Cleaned up internal-deployment naming in `README.md`, `RELEASE.md`,
  and source-code comments / docstrings. Replaced references to a
  specific internal backend service name with neutral phrasing
  ("the ClicheFactory service" / "the backend"). No code paths
  changed.
- Moved the post-publish "bump the floor in downstream consumers" step
  out of `RELEASE.md`. That step is operator-internal and lives in
  the local release runbook now; this file is purely "how to publish
  to PyPI".

## [0.4.2] — 2026-05-10

### Documentation

- Reframe the service-mode README to match the new default. The SDK has
  targeted `https://api.clichefactory.com` since 0.4.1, but the README
  still read like local dev was the happy path and `CLICHEFACTORY_API_URL`
  was something you set "for production". It's the other way around now:
  install, set `CLICHEFACTORY_API_KEY`, call `factory()` — same shape as
  the OpenAI / Anthropic SDKs. `CLICHEFACTORY_API_URL` is the override
  for local aio-server development and self-hosting.

## [0.4.1] — 2026-05-10

### Changed

- **Service-mode default base URL is now `https://api.clichefactory.com`**
  (was `http://127.0.0.1:4000`). The aio-server is live in production at
  this hostname, so installs of the SDK with no configuration now talk to
  the public API by default. Local development must set
  `CLICHEFACTORY_API_URL` (e.g. `http://localhost:4000`, or
  `http://aio-server:8000` inside the Docker dev network) to point the
  SDK back at a local instance. The explicit `Cliche(base_url=...)` /
  CLI `--base-url` overrides and `~/.clichefactory/config.toml`
  precedence are unchanged.

## [0.4.0] — 2026-05-09

### Added

- **Fast mode now works for every format we have a parser for.** The
  `mode="fast"` / `mode="one-shot"` extract path used to send raw bytes
  directly to the LLM and would fail with a vendor `400 INVALID_ARGUMENT`
  when the MIME was not supported (e.g. `message/rfc822` for emails,
  Office formats, CSV). The SDK now pre-flights via the new
  `AIClient.supports_bytes(mime)` capability check and, when the vendor
  cannot accept the bytes directly, transparently parses the file to
  markdown locally and runs a single
  `client.extract(text=markdown, schema=...)` call. Fast-mode semantics
  are preserved (one LLM call for the extraction itself, no DSPy
  pipeline). PDFs and common image MIMEs (`image/jpeg|png|gif|webp`)
  continue to use the multimodal raw-bytes path unchanged.
- **`AIClient.supports_bytes(mime)`** added to the protocol. Each
  built-in client (`GeminiAIClient`, `OpenAIAIClient`,
  `AnthropicAIClient`, `OllamaAIClient`) implements it. Ollama always
  returns `False` (no multimodal support in MVP).
- **`UnsupportedBytesMimeError`** raised by `extract_from_bytes` when
  callers bypass the capability check and pass an unsupported MIME.
  Replaces the cryptic raw vendor `400` for that case.
- **`is_default_direct_bytes_mime` / `client_supports_bytes`** helper
  functions exported from `clichefactory._engine.ai_clients` for callers
  building their own routing (e.g. `aio-server` extraction service). The
  free `client_supports_bytes(client, mime)` falls back to a conservative
  default (PDF + common image MIMEs) when a BYO client does not
  implement `supports_bytes`.

### Notes for consumers

- No public `Cliche.extract` argument changed. Existing callers running
  `mode="fast"` on EML / DOCX / XLSX / CSV / ODT / DOC will start
  succeeding instead of raising. Latency increases by the local parse
  step (typically <300 ms for emails and small office docs); LLM call
  count is unchanged.
- BYO `AIClient` implementations that do not declare `supports_bytes`
  keep working — the SDK falls back to a default heuristic. Implementing
  `supports_bytes` is recommended so vendor-specific MIME support can be
  declared explicitly.

## [0.3.0] — 2026-05-01

### Added

- **Automatic retries on transient service errors.** Service-mode HTTP
  callsites (`extract`, `to_markdown`, presigned upload, presigned PUT)
  now retry on connection / read errors and on `408`, `425`, `429`,
  `500`, `502`, `503`, `504` responses. Bounded at 4 attempts with
  full-jitter exponential backoff (max 8 s per sleep). When the server
  sends `Retry-After` (e.g. when rate-limited), the SDK honors it,
  clamped to 30 s so a misbehaving upstream can't park a request
  indefinitely. Non-retryable 4xx responses (auth, validation,
  not-found) still fail fast as before.
- **Idempotency keys on retries.** `extract` already derived an
  idempotency key from the request payload and sent it inside the
  canonical envelope; the same key is now reused on every retry so the
  server replays the cached response instead of double-billing. The
  `to_markdown` and presigned-upload requests now also derive a stable
  key and send it as the `Idempotency-Key` HTTP header. The presigned
  S3 PUT itself is naturally idempotent on URL+content, so no key is
  required there — retries just cover transport hiccups.

### Notes for consumers

- No public API changed. The retry / idempotency wiring is internal to
  the service-mode transport layer; existing call sites get the new
  behavior automatically on upgrade.
- Users wanting to pin their own idempotency key (today the SDK derives
  one from the request body) — that knob is still deferred until a real
  trigger surfaces.

## [0.2.2] — 2026-04-29

### Fixed
- **OpenAI client**: switched from `max_tokens` to `max_completion_tokens` for
  compatibility with newer OpenAI models (GPT-5.x, o-series) that reject the
  legacy parameter. Reasoning models (o1/o3/o4 prefixes) now also have
  `temperature` omitted, since they reject it.
- **Anthropic client**: structured output schemas now have
  `additionalProperties: false` recursively applied to all object nodes,
  required by the Anthropic Messages API for `json_schema` outputs.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor bumps may introduce additive public API, patch bumps are
bugfix/docs only).

## [0.2.0] — 2026-04-19

Long-document extraction: chunk a document, extract per chunk, merge results
into a single Pydantic model. Purely additive; no changes to existing APIs.

### Added

- **`Cliche.extract_long` / `extract_long_async`** — new top-level entry point
  for documents that exceed the single-call LLM context window (typically
  longer than ~20 pages). Runs `to_markdown` once, chunks the markdown,
  extracts each chunk in parallel via the existing `extract(text=...)` path,
  and merges per-field using a configurable resolver.
- **`clichefactory.chunking`** — public chunking strategies:
  - `TokenChunker` (default) — token-budgeted splits with configurable overlap.
  - `PageChunker` — splits on markdown page markers; falls back to
    `TokenChunker` when the document has no detectable page breaks (emits a
    warning so the fallback is visible).
  - `HeadingChunker` — splits on markdown headings, re-chunking oversize
    sections with `TokenChunker`.
  - `ChunkStrategy` protocol for custom chunkers.
- **`clichefactory.resolvers`** — built-in per-field merge strategies:
  - Scalar: `first_non_null`, `last_non_null`, `most_common`,
    `pick_by_confidence`.
  - Numeric: `sum_numeric`, `max_numeric`, `min_numeric`.
  - Collection: `concat`, `concat_dedupe`, `union_by`.
  - `llm_reconcile` — v1 stub; currently falls back to `most_common`, full
    LLM-driven reconciliation planned for a subsequent release.
  - String aliases (`"first_non_null"`, `"concat_dedupe"`,
    `"concat_dedupe_by=invoice_number"`, …) for config-driven setups.
- **`resolvers=` keyword** on `Client.cliche()` and `Cliche(...)` — mirrors
  the existing `postprocess` hook, so per-field merge rules can be set
  once at the cliche level and overridden per `extract_long` call.
- **Default resolver policy** — driven by the target Pydantic model's JSON
  schema: arrays → `concat` with a **loud warning** recommending
  `concat_dedupe` or `concat_dedupe_by=<attr>`; scalars / objects →
  `first_non_null`.
- **New public types**: `Chunk`, `FieldValue`, `ResolverContext`,
  `Resolver`, `ResolverFn`, `ResolverSpec`, `ResolutionTrace`,
  `LongExtractionResult` (returned when `include_chunk_results=True`,
  exposing per-chunk results, per-field values, resolution traces,
  warnings and aggregated cost).
- **New exception**: `LongExtractionError` with a code taxonomy:
  `long.chunker_failed`, `long.no_chunks`, `long.all_chunks_failed`,
  `long.resolver_failed`, `long.unsupported_mode`,
  `long.unknown_resolver`, `long.invalid_resolver`.
- **README**: new "Long documents (chunk + merge)" section with usage,
  chunker and resolver tables, default-policy explanation, and the debug
  surface of `LongExtractionResult`.
- **Tests**: 52 new unit tests covering resolvers, chunkers, default policy,
  and end-to-end orchestration with mocked `to_markdown` / `extract`.

### Limitations (v1)

- `extract_long` only runs the BYOK one-shot `extract(text=...)` path per
  chunk. `mode="trained" | "robust" | "robust-trained"` and `artifact_id=`
  are explicitly rejected with `LongExtractionError("long.unsupported_mode")`
  until we add per-chunk support for them.
- `llm_reconcile` is a stub (see above).
- No server-side `extraction_mode="long"` yet — this release is SDK-only.
  A server implementation will be considered once Emio UI demand validates
  the resolver patterns.

## [0.1.0] — 2026-04-12

- Initial public release.
