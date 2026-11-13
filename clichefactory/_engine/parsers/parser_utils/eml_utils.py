# ---- Utility helpers for eml parsing
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
import re
from typing import Dict, Iterable, Iterator, List, Optional

import logging
from clichefactory._engine.models.normalized_doc import NormalizedDoc

logger = logging.getLogger(__name__)


# ---- Which parts of an email are documents

# Inline images below this size that carry a Content-ID are logos and icons the
# HTML body references (``cid:image001.png``), mostly in signatures. A reply
# chain can carry a dozen. They are not documents: not parsed, and not billed by
# the ClicheFactory service. A screenshot pasted into the body is inline with a
# Content-ID too, but much larger, so it is still read.
INLINE_IMAGE_MAX_BYTES = 20 * 1024

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_DOCUMENT_DISPOSITIONS = (None, "inline", "attachment")


@dataclass(frozen=True)
class DocumentPart:
    """One document found in an email: a file attachment or a forwarded email."""

    filename: str
    extension: str  # lowercase, with the dot: ".pdf", ".eml"
    content: bytes


def walk_own_parts(msg: Message) -> Iterator[Message]:
    """Like ``msg.walk()``, but without walking into forwarded emails.

    A forwarded email (``message/rfc822``) is yielded as one part; its body and
    attachments belong to it and are read when it is parsed as an email.
    """
    yield msg
    if msg.get_content_type() == "message/rfc822":
        return
    if msg.is_multipart():
        for sub in msg.get_payload():
            yield from walk_own_parts(sub)


def iter_document_parts(msg: Message, extensions: Iterable[str]) -> Iterator[DocumentPart]:
    """Each part of ``msg`` that is a document, exactly once.

    This is the one rule for which parts of an email are read. ``EmlParser``
    parses these parts, and the ClicheFactory service bills exactly these (1
    page for the email, plus each of these by its own page rule), so keep both
    on this function.

    A part is a document when it:

    * has a filename whose extension is in ``extensions`` (the registered
      parsers),
    * has a disposition of none, ``inline`` or ``attachment``,
    * isn't empty, and
    * isn't a small inline image with a Content-ID (a signature logo).

    A forwarded email (``message/rfc822``) is one ``.eml`` document, filename
    or not, and isn't walked into: parsing it as an email reads its body and
    attachments, so nothing inside it is read twice.

    A part that can't be decoded is skipped with a warning.
    """
    exts = frozenset(e.lower() for e in extensions)
    for part in walk_own_parts(msg):
        try:
            doc = _document_part(part, exts)
        except Exception as exc:  # malformed headers or payload
            logger.warning("Skipping unreadable email part: %s", exc)
            continue
        if doc is not None:
            yield doc


def _document_part(part: Message, exts: frozenset[str]) -> Optional[DocumentPart]:
    forwarded = part.get_content_type() == "message/rfc822"
    if part.is_multipart() and not forwarded:
        return None
    if part.get_content_disposition() not in _DOCUMENT_DISPOSITIONS:
        return None

    if forwarded:
        if ".eml" not in exts:
            return None
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if inner is None:
            return None
        filename = part.get_filename() or "forwarded.eml"
        if Path(filename).suffix.lower() != ".eml":
            filename += ".eml"
        return DocumentPart(filename, ".eml", inner.as_bytes())

    filename = part.get_filename()
    if not filename:
        return None
    ext = Path(filename).suffix.lower()
    if ext not in exts:
        return None
    content = part.get_payload(decode=True)
    if not content:
        logger.warning("Empty attachment content for %s", filename)
        return None
    if (
        ext in _IMAGE_EXTENSIONS
        and part.get("Content-ID")
        and len(content) < INLINE_IMAGE_MAX_BYTES
    ):
        return None
    return DocumentPart(filename, ext, content)

def normalize_newlines(text: str) -> str:
    """RFC 5322–compliant email messages must use CRLF (\r\n) as line endings.
    Python’s email package preserves those line endings in most body extraction paths 
    unless you normalize them.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,})(?P<space>\s+)(?P<title>.*?)(?P<trailing>\s*#*\s*)$")

def demote_headings(markdown: str, *, delta: int = 1, max_level: int = 6) -> str:
    """
    Demote headings (#, ##, ###...) by `delta`.
    - Ignores headings inside fenced code blocks.
    - Any resulting level > max_level becomes bold text: **Title** (instead of ####### Title).
    """
    if not markdown:
        return ""

    out: List[str] = []
    in_fence = False

    for line in markdown.splitlines():
        stripped = line.lstrip()

        # Track fenced code blocks (``` or ~~~). Simple + robust enough for most outputs.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue

        if in_fence:
            out.append(line)
            continue

        m = _HEADING_RE.match(line)
        if not m:
            out.append(line)
            continue

        old_level = len(m.group("hashes"))
        new_level = old_level + delta
        title = m.group("title").strip()

        if new_level > max_level:
            # Turn "too-deep headings" into bold paragraphs.
            # Keep it simple and predictable.
            out.append(f"**{title}**")
        else:
            out.append(f"{'#' * new_level} {title}")

    return "\n".join(out)


def build_plain_text(headers: Message, body_plain: Optional[str], attachments: Optional[List[NormalizedDoc]]) -> str:
    lines: List[str] = []

    subject = headers.get("Subject", "(no subject)")
    lines.append(subject)
    lines.append("")

    # Headers as plain text (no heading)
    for name in ["From", "To", "Cc", "Bcc", "Date", "Message-ID"]:
        val = headers.get(name)
        if val:
            lines.append(f"{name}: {val}")

    if body_plain:
        lines.append("")
        lines.append(body_plain.strip())

    if attachments:
        for i, att in enumerate(attachments, start=1):
            lines.append("")
            lines.append(f"[Attachment {i}]")
            lines.append(att.get_plain_text().strip())

    return "\n".join(lines).strip() + "\n"


def build_markdown(headers: Message, body_plain: Optional[str], attachments: Optional[List[NormalizedDoc]]) -> str:
    md: List[str] = []

    subject = headers.get("Subject", "(no subject)")
    md.append(f"# {subject}")
    md.append("")

    # Headers as plain text (NOT headings)
    header_lines: List[str] = []
    for name in ["From", "To", "Cc", "Bcc", "Date", "Message-ID"]:
        val = headers.get(name)
        if val:
            header_lines.append(f"**{name}:** {val}")

    if header_lines:
        md.extend(header_lines)
        md.append("")

    # Body as plain text
    if body_plain and body_plain.strip():
        md.append("## Email body")
        md.append(body_plain.strip())
        md.append("")

    # Attachments: each is an H2, inner headings demoted by +1
    if attachments:
        md.append("# Email attachments")
        for idx, att in enumerate(attachments, start=1):
            title = getattr(att, "filename", None) or f"Attachment {idx}"
            md.append(f"## {title}")
            md.append("")

            att_md = (att.get_markdown() or "").strip()
            if att_md:
                md.append(demote_headings(att_md, delta=2, max_level=6))
            else:
                md.append("_Empty attachment_")

            md.append("")

    if not (header_lines or (body_plain and body_plain.strip()) or attachments):
        md.append("_No email content available_")
        md.append("")

    return "\n".join(md).rstrip() + "\n"


def extract_body(msg: Message) -> Dict[str, Optional[str]]:
    """
    Prefer text/plain; fall back to text/html (with very naive tag stripping).
    Returns dict with 'text_plain' and 'text_html' keys.
    """
    text_plain = None
    text_html = None

    if msg.is_multipart():
        # A forwarded email's body is its own; it is read when the forwarded
        # email is parsed as an attachment (see iter_document_parts).
        for part in walk_own_parts(msg):
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue  # skip attachments here

            try:
                payload = part.get_content()  # type: ignore
            except Exception:
                continue  # TODO: Should we continue or throw?

            if content_type == "text/plain" and text_plain is None and isinstance(payload, str):
                text_plain = normalize_newlines(payload)
            elif content_type == "text/html" and text_html is None and isinstance(payload, str):
                text_html = normalize_newlines(payload)
    else:
        content_type = msg.get_content_type()
        try:
            payload = msg.get_content() # type: ignore
        except Exception:
            payload = None
        if content_type == "text/plain" and isinstance(payload, str):
            text_plain = normalize_newlines(payload)
        elif content_type == "text/html" and isinstance(payload, str):
            text_html = normalize_newlines(payload)

    # Fallback: crude HTML->text if plain missing
    if text_plain is None and text_html is not None:
        import re
        # Very naive stripping – replace <br> with newlines, then strip tags.
        text = text_html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        text = re.sub(r"<[^>]+>", "", text)
        text_plain = text

    return {"text_plain": text_plain, "text_html": text_html}

