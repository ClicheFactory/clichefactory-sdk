"""
ClicheFactory CLI.

Usage:
    clichefactory configure          Interactive first-time setup
    clichefactory extract            Extract structured data from a document
    clichefactory extract-batch      Extract from multiple documents
    clichefactory to-markdown        Convert a document to markdown
    clichefactory to-markdown-batch  Convert multiple documents to markdown
    clichefactory doctor             Check dependencies and configuration
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from clichefactory._config import (
    CLIConfig,
    config_file_path,
    load_config,
    resolve_api_key,
    resolve_base_url,
    resolve_model,
    resolve_model_api_key,
    resolve_ocr_api_key,
    resolve_ocr_model,
    save_config,
)

__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(msg: str, *, hint: str | None = None) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    if hint:
        print(f"Hint: {hint}", file=sys.stderr)
    sys.exit(1)


def _load_schema(path: str) -> dict[str, Any]:
    """Load a JSON schema file and return the parsed dict."""
    p = Path(path)
    if not p.is_file():
        _error(f"Schema file not found: {path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _error(f"Invalid JSON in schema file: {e}")
    return {}  # unreachable


def _build_parsing_options(args: argparse.Namespace) -> Any:
    """Build ParsingOptions from CLI flags, or None if no parsing flags set."""
    from clichefactory import ParsingOptions

    kwargs: dict[str, Any] = {}
    if getattr(args, "ocr_engine", None):
        kwargs["pdf_ocr_engine"] = args.ocr_engine
        _IMAGE_PARSER_MAP = {"tesseract": "pytesseract", "rapidocr": "rapidocr"}
        if args.ocr_engine in _IMAGE_PARSER_MAP:
            kwargs["image_parser"] = _IMAGE_PARSER_MAP[args.ocr_engine]
    if getattr(args, "lang", None):
        kwargs["pdf_ocr_lang"] = args.lang
        kwargs["image_parser_lang"] = args.lang
    return ParsingOptions(**kwargs) if kwargs else None


def _build_client(args: argparse.Namespace, cfg: CLIConfig) -> Any:
    """Build a clichefactory Client from resolved config."""
    from clichefactory import Endpoint, factory

    mode = getattr(args, "client_mode", None) or cfg.default_mode

    if mode == "service":
        api_key = resolve_api_key(cli_flag=getattr(args, "api_key", None), cfg=cfg)
        if not api_key:
            _error(
                "No ClicheFactory API key configured.",
                hint='Run "clichefactory configure" or pass --api-key.',
            )
        base_url = resolve_base_url(cli_flag=getattr(args, "base_url", None), cfg=cfg)
        model_name = resolve_model(cli_flag=getattr(args, "model", None), cfg=cfg)
        model_key = resolve_model_api_key(cli_flag=getattr(args, "model_api_key", None), cfg=cfg)

        model_ep = None
        if model_name:
            model_ep = Endpoint(provider_model=model_name, api_key=model_key or None)

        return factory(
            api_key=api_key,
            base_url=base_url,
            mode="service",
            model=model_ep,
            parsing=_build_parsing_options(args),
        )

    # Local mode
    model_name = resolve_model(cli_flag=getattr(args, "model", None), cfg=cfg)
    model_key = resolve_model_api_key(cli_flag=getattr(args, "model_api_key", None), cfg=cfg)

    if not model_name:
        _error(
            "No LLM model configured for local mode.",
            hint='Run "clichefactory configure --local" or pass --model.',
        )

    model_ep = Endpoint(provider_model=model_name, api_key=model_key or None)

    ocr_model_name = resolve_ocr_model(cli_flag=getattr(args, "ocr_model", None), cfg=cfg)
    ocr_key = resolve_ocr_api_key(
        cli_flag=getattr(args, "ocr_api_key", None), cfg=cfg, model_api_key=model_key
    )
    ocr_ep = None
    if ocr_model_name:
        ocr_ep = Endpoint(provider_model=ocr_model_name, api_key=ocr_key or None)

    return factory(
        mode="local",
        model=model_ep,
        ocr_model=ocr_ep,
        parsing=_build_parsing_options(args),
    )


def _write_output(data: Any, output: str | None, *, is_json: bool = True) -> None:
    """Write result to file or stdout."""
    if is_json:
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    else:
        text = str(data)

    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
        print(f"Written to {output}")
    else:
        print(text)


# ---------------------------------------------------------------------------
# Shared argument groups
# ---------------------------------------------------------------------------

def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add connection/auth arguments shared by all commands."""
    group = parser.add_argument_group("connection")
    group.add_argument(
        "--mode", dest="client_mode", choices=["local", "service"],
        help="Execution mode (default: from config file)",
    )
    group.add_argument("--api-key", dest="api_key", help="ClicheFactory service API key")
    group.add_argument("--base-url", dest="base_url", help="Service base URL")
    group.add_argument("--model", help="LLM model name (e.g. openai/gpt-4o, gemini/gemini-3-flash-preview)")
    group.add_argument("--model-api-key", dest="model_api_key", help="LLM API key")
    group.add_argument("--ocr-model", dest="ocr_model", help="OCR/VLM model name (optional, defaults to main model)")
    group.add_argument("--ocr-api-key", dest="ocr_api_key", help="OCR model API key (optional, defaults to main key)")


def _add_parsing_args(parser: argparse.ArgumentParser) -> None:
    """Add parsing/OCR arguments shared by extract and to-markdown commands."""
    group = parser.add_argument_group("parsing")
    group.add_argument(
        "--ocr-engine", dest="ocr_engine",
        choices=["tesseract", "rapidocr", "easyocr"],
        help="OCR engine for PDF and image parsing (default: rapidocr)",
    )
    group.add_argument(
        "--lang", help='OCR language in Tesseract format, e.g. "eng", "deu+eng" (default: eng)',
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_configure(args: argparse.Namespace) -> None:
    """Interactive configuration setup."""
    cfg = load_config()

    if args.local:
        print("--- Local mode configuration ---")
        print("Configure the LLM used for extraction and OCR.\n")

        model = input(f"LLM model name (e.g. openai/gpt-4o) [{cfg.local.model}]: ").strip()
        if model:
            cfg.local.model = model

        api_key = input(f"LLM API key [{_mask(cfg.local.api_key)}]: ").strip()
        if api_key:
            cfg.local.api_key = api_key

        print("\nOptional: separate OCR model (press Enter to skip — main model will be used)")
        ocr_model = input(f"OCR model name [{cfg.local.ocr_model}]: ").strip()
        if ocr_model:
            cfg.local.ocr_model = ocr_model
            ocr_key = input(f"OCR API key [{_mask(cfg.local.ocr_api_key)}]: ").strip()
            if ocr_key:
                cfg.local.ocr_api_key = ocr_key

        cfg.default_mode = "local"
    else:
        print("--- ClicheFactory service configuration ---\n")

        api_key = input(f"ClicheFactory API key [{_mask(cfg.service.api_key)}]: ").strip()
        if api_key:
            cfg.service.api_key = api_key

        print("\nOptional: BYOK model override (press Enter to skip — service uses hosted models)")
        model = input(f"LLM model name [{cfg.local.model}]: ").strip()
        if model:
            cfg.local.model = model
            model_key = input(f"LLM API key [{_mask(cfg.local.api_key)}]: ").strip()
            if model_key:
                cfg.local.api_key = model_key

        cfg.default_mode = "service"

    path = save_config(cfg)
    print(f"\nConfig saved to {path}")


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "***"
    return s[:4] + "..." + s[-4:]


def _format_cost(cost: Any) -> str:
    pages = f"{cost.pages} page{'s' if cost.pages != 1 else ''}"
    credits = f"{cost.credits_used} credit{'s' if cost.credits_used != 1 else ''}"
    rate = f" ({cost.credits_per_page}/page)" if cost.credits_per_page else ""
    return f"Cost: {pages}, {credits}{rate}"


def _pages_text(rate: Any) -> str:
    return "unlimited" if rate.pages is None else f"{rate.pages:,}"


def _format_balance(bal: Any) -> str:
    currency = f" ({bal.currency})" if bal.currency else ""
    lines = [f"Balance: {bal.balance_credits:,} credits{currency}"]
    if bal.free_extraction_pages_remaining:
        lines.append(f"Free extraction pages left: {bal.free_extraction_pages_remaining:,}")
    pr = bal.pages_remaining
    if pr is not None:
        def per_mode(rates: dict[str, Any]) -> str:
            return ", ".join(f"{mode} {_pages_text(r)}" for mode, r in rates.items())

        lines.append("Pages left at current rates:")
        if pr.extract:
            lines.append(f"  extract         {per_mode(pr.extract)}")
        if pr.extract_byok:
            lines.append(f"  extract (BYOK)  {per_mode(pr.extract_byok)}")
        if pr.to_markdown is not None:
            lines.append(f"  to-markdown     {_pages_text(pr.to_markdown)}")
    return "\n".join(lines)


def cmd_balance(args: argparse.Namespace) -> None:
    """Show the credit balance of the API key's account."""
    from clichefactory.errors import ClicheFactoryError

    cfg = load_config()
    args.client_mode = "service"  # the balance lives on the service, whatever the default mode
    try:
        client = _build_client(args, cfg)
        balance = client.balance()
    except ClicheFactoryError as e:
        _error(str(e), hint=getattr(e.info, "hint", None))
        return
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)

    if args.json:
        _write_output(balance.model_dump(mode="json"), None)
    else:
        print(_format_balance(balance))


def cmd_extract(args: argparse.Namespace) -> None:
    """Extract structured data from a single document."""
    from clichefactory.errors import ClicheFactoryError

    cfg = load_config()

    schema = _load_schema(args.schema)
    file_path = args.file

    if not Path(file_path).is_file():
        _error(f"File not found: {file_path}")

    try:
        client = _build_client(args, cfg)
        artifact_id = getattr(args, "artifact_id", None)
        cliche = client.cliche(schema, artifact_id=artifact_id)

        extraction_mode = getattr(args, "extraction_mode", None)
        if getattr(args, "show_cost", False):
            result, cost = cliche.extract(file=file_path, mode=extraction_mode, return_cost=True)
            print(_format_cost(cost), file=sys.stderr)
        else:
            result = cliche.extract(file=file_path, mode=extraction_mode)

        if hasattr(result, "model_dump"):
            data = result.model_dump(mode="json")
        elif hasattr(result, "raw"):
            data = {"raw": result.raw, "validation_errors": result.validation_errors}
        else:
            data = result

        _write_output(data, args.output)

    except ClicheFactoryError as e:
        _error(str(e), hint=getattr(e.info, "hint", None))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


def cmd_extract_batch(args: argparse.Namespace) -> None:
    """Extract structured data from multiple documents."""
    from clichefactory.errors import ClicheFactoryError

    cfg = load_config()
    schema = _load_schema(args.schema)
    files = args.files

    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        _error(f"Files not found: {', '.join(missing)}")

    try:
        client = _build_client(args, cfg)
        artifact_id = getattr(args, "artifact_id", None)
        cliche = client.cliche(schema, artifact_id=artifact_id)

        extraction_mode = getattr(args, "extraction_mode", None)
        results = cliche.extract_batch(
            files=files,
            mode=extraction_mode,
            max_concurrency=args.max_concurrency,
        )

        output_data = []
        for i, result in enumerate(results):
            if hasattr(result, "model_dump"):
                output_data.append({"file": files[i], "result": result.model_dump(mode="json")})
            elif isinstance(result, Exception):
                output_data.append({"file": files[i], "error": str(result)})
            else:
                output_data.append({"file": files[i], "result": result})

        _write_output(output_data, args.output)

    except ClicheFactoryError as e:
        _error(str(e), hint=getattr(e.info, "hint", None))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


def cmd_to_markdown(args: argparse.Namespace) -> None:
    """Convert a document to markdown."""
    from clichefactory.errors import ClicheFactoryError

    cfg = load_config()
    file_path = args.file

    if not Path(file_path).is_file():
        _error(f"File not found: {file_path}")

    try:
        client = _build_client(args, cfg)
        doc = client.to_markdown(
            file=file_path,
            conversion_mode=getattr(args, "conversion_mode", None),
        )

        text = doc.get_markdown() if hasattr(doc, "get_markdown") else str(doc)
        _write_output(text, args.output, is_json=False)

    except ClicheFactoryError as e:
        _error(str(e), hint=getattr(e.info, "hint", None))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


def cmd_to_markdown_batch(args: argparse.Namespace) -> None:
    """Convert multiple documents to markdown."""
    from clichefactory.errors import ClicheFactoryError

    cfg = load_config()
    files = args.files

    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        _error(f"Files not found: {', '.join(missing)}")

    try:
        client = _build_client(args, cfg)
        docs = client.to_markdown_batch(
            files=files,
            conversion_mode=getattr(args, "conversion_mode", None),
            max_concurrency=args.max_concurrency,
        )

        if args.output:
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            for f, doc in zip(files, docs):
                stem = Path(f).stem
                md = doc.get_markdown() if hasattr(doc, "get_markdown") else str(doc)
                dest = out_dir / f"{stem}.md"
                dest.write_text(md, encoding="utf-8")
                print(f"  {f} -> {dest}")
        else:
            for f, doc in zip(files, docs):
                md = doc.get_markdown() if hasattr(doc, "get_markdown") else str(doc)
                print(f"--- {f} ---")
                print(md)
                print()

    except ClicheFactoryError as e:
        _error(str(e), hint=getattr(e.info, "hint", None))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check dependencies, system binaries, and configuration."""
    ok_count = 0
    warn_count = 0
    err_count = 0

    def ok(msg: str) -> None:
        nonlocal ok_count
        ok_count += 1
        print(f"  [OK]   {msg}")

    def warn(msg: str) -> None:
        nonlocal warn_count
        warn_count += 1
        print(f"  [WARN] {msg}")

    def err(msg: str) -> None:
        nonlocal err_count
        err_count += 1
        print(f"  [ERR]  {msg}")

    def info(msg: str) -> None:
        # Purely informational lines (e.g. "optional dep not installed").
        # Not counted against ok/warn/err totals.
        print(f"  [INFO] {msg}")

    print("ClicheFactory Doctor\n")

    # --- Config file ---
    print("Configuration:")
    cfg_path = config_file_path()
    if cfg_path.is_file():
        cfg = load_config()
        ok(f"Config file: {cfg_path}")
        if cfg.default_mode == "service" and cfg.service.api_key:
            ok(f"Service API key configured ({_mask(cfg.service.api_key)})")
        elif cfg.default_mode == "service":
            warn("Service mode selected but no API key configured")
        if cfg.default_mode == "local" and cfg.local.model:
            ok(f"Local model: {cfg.local.model}")
        elif cfg.default_mode == "local":
            warn("Local mode selected but no model configured")
    else:
        warn(f'No config file found. Run "clichefactory configure" to set up.')

    print()

    # --- Python dependencies ---
    print("Python dependencies:")

    core_deps = [
        ("httpx", "httpx"),
        ("pydantic", "pydantic"),
        ("anyio", "anyio"),
    ]
    for name, module in core_deps:
        try:
            __import__(module)
            ok(name)
        except ImportError:
            err(f"{name} (not installed)")

    local_deps = [
        ("pymupdf", "fitz", "pip install clichefactory[local]"),
        ("docling", "docling", "pip install clichefactory[local]"),
        ("Pillow", "PIL", "pip install clichefactory[local]"),
        ("RapidOCR", "rapidocr", "pip install clichefactory[local]"),
        ("pytesseract", "pytesseract", "pip install clichefactory[local]"),
        ("openpyxl", "openpyxl", "pip install clichefactory[local]"),
        ("python-docx", "docx", "pip install clichefactory[local]"),
        ("pypdf", "pypdf", "pip install clichefactory[local]"),
    ]
    for name, module, install in local_deps:
        try:
            __import__(module)
            ok(f"{name} (local)")
        except ImportError:
            warn(f"{name} not installed (needed for local mode: {install})")

    optional_deps = [
        ("easyocr", "easyocr", "pip install easyocr"),
    ]
    for name, module, install in optional_deps:
        try:
            __import__(module)
            ok(f"{name} (optional)")
        except ImportError:
            warn(f"{name} not installed (optional: {install})")

    print()

    # --- System binaries ---
    print("System binaries:")

    tesseract_path = shutil.which("tesseract")
    if tesseract_path:
        ok(f"tesseract: {tesseract_path}")
    else:
        warn("tesseract not found on PATH (needed for tesseract OCR engine)")

    soffice_path = shutil.which("soffice")
    if soffice_path:
        ok(f"soffice (LibreOffice): {soffice_path}")
    else:
        warn("soffice not found on PATH (needed for .doc / .odt conversion)")

    # pandoc is an optional fallback path inside office_converter; the
    # default deployment runs soffice for both .doc and .odt and never
    # touches pandoc. Surfaced here for setups that prefer pandoc.
    pandoc_path = shutil.which("pandoc")
    if pandoc_path:
        ok(f"pandoc: {pandoc_path}")
    else:
        info("pandoc not found on PATH (optional; soffice handles .doc / .odt)")

    print()

    # --- RapidOCR language support ---
    print("RapidOCR language support:")
    try:
        from rapidocr import LangRec
        langs = [e.value for e in LangRec]
        ok(f"LangRec available: {', '.join(langs)}")
    except ImportError:
        warn("RapidOCR not installed — language check skipped")
    except Exception:
        warn("RapidOCR installed but LangRec not available (version may be < 3.4)")

    print()

    # --- Summary ---
    total = ok_count + warn_count + err_count
    print(f"Summary: {ok_count}/{total} checks passed, {warn_count} warnings, {err_count} errors")

    if err_count > 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clichefactory",
        description="ClicheFactory CLI — structured data extraction from documents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", title="commands")

    # --- configure ---
    p_configure = subparsers.add_parser(
        "configure", help="Interactive first-time setup",
    )
    p_configure.add_argument(
        "--local", action="store_true",
        help="Configure for local mode (LLM model + keys)",
    )

    # --- extract ---
    p_extract = subparsers.add_parser(
        "extract", help="Extract structured data from a document",
    )
    p_extract.add_argument("file", help="Path to the document file")
    p_extract.add_argument(
        "--schema", required=True,
        help="Path to a JSON schema file describing the extraction target",
    )
    p_extract.add_argument(
        "--extraction-mode", dest="extraction_mode",
        choices=["fast", "trained", "robust", "robust-trained"],
        help="Extraction mode (default: standard OCR+extract)",
    )
    p_extract.add_argument(
        "--artifact-id", dest="artifact_id",
        help="Trained artifact ID (from Emio)",
    )
    p_extract.add_argument("-o", "--output", help="Output file path (default: stdout)")
    p_extract.add_argument(
        "--show-cost", dest="show_cost", action="store_true",
        help="Print the pages and credits this extraction cost to stderr. Service mode only.",
    )
    _add_connection_args(p_extract)
    _add_parsing_args(p_extract)

    # --- extract-batch ---
    p_extract_batch = subparsers.add_parser(
        "extract-batch", help="Extract structured data from multiple documents",
    )
    p_extract_batch.add_argument("files", nargs="+", help="Paths to document files")
    p_extract_batch.add_argument(
        "--schema", required=True,
        help="Path to a JSON schema file describing the extraction target",
    )
    p_extract_batch.add_argument(
        "--extraction-mode", dest="extraction_mode",
        choices=["fast", "trained", "robust", "robust-trained"],
        help="Extraction mode",
    )
    p_extract_batch.add_argument(
        "--max-concurrency", type=int, default=5,
        help="Max parallel extractions (default: 5)",
    )
    p_extract_batch.add_argument("-o", "--output", help="Output file path (default: stdout)")
    _add_connection_args(p_extract_batch)
    _add_parsing_args(p_extract_batch)

    # --- to-markdown ---
    p_md = subparsers.add_parser(
        "to-markdown", help="Convert a document to markdown",
    )
    p_md.add_argument("file", help="Path to the document file")
    p_md.add_argument(
        "--conversion-mode", dest="conversion_mode",
        choices=["default", "fast"],
        help="Conversion mode: default (full OCR pipeline) or fast (VLM-only, no OCR). Service mode only.",
    )
    p_md.add_argument("-o", "--output", help="Output file path (default: stdout)")
    _add_connection_args(p_md)
    _add_parsing_args(p_md)

    # --- to-markdown-batch ---
    p_md_batch = subparsers.add_parser(
        "to-markdown-batch", help="Convert multiple documents to markdown",
    )
    p_md_batch.add_argument("files", nargs="+", help="Paths to document files")
    p_md_batch.add_argument(
        "--conversion-mode", dest="conversion_mode",
        choices=["default", "fast"],
        help="Conversion mode: default (full OCR pipeline) or fast (VLM-only, no OCR). Service mode only.",
    )
    p_md_batch.add_argument(
        "--max-concurrency", type=int, default=5,
        help="Max parallel conversions (default: 5)",
    )
    p_md_batch.add_argument(
        "-o", "--output",
        help="Output directory (one .md file per input; default: stdout)",
    )
    _add_connection_args(p_md_batch)
    _add_parsing_args(p_md_batch)

    # --- balance ---
    p_balance = subparsers.add_parser(
        "balance", help="Show your credit balance and how many pages it buys",
    )
    p_balance.add_argument("--json", action="store_true", help="Print the full balance as JSON")
    p_balance.add_argument("--api-key", dest="api_key", help="ClicheFactory service API key")
    p_balance.add_argument("--base-url", dest="base_url", help="Service base URL")

    # --- doctor ---
    subparsers.add_parser("doctor", help="Check dependencies and configuration")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "configure": cmd_configure,
        "extract": cmd_extract,
        "extract-batch": cmd_extract_batch,
        "to-markdown": cmd_to_markdown,
        "to-markdown-batch": cmd_to_markdown_batch,
        "balance": cmd_balance,
        "doctor": cmd_doctor,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
