"""Which parts of an email are documents, and that EmlParser reads each one once.

``iter_document_parts`` is shared with the ClicheFactory service, which bills an
email as 1 page plus exactly these parts (aio-server ``page_counter``).
"""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from clichefactory._engine.parsers.eml_parser import EmlParser
from clichefactory._engine.parsers.media_parser_registry import MediaParserRegistry
from clichefactory._engine.parsers.parser_utils.eml_utils import (
    INLINE_IMAGE_MAX_BYTES,
    iter_document_parts,
)
from clichefactory._engine.parsers.parser_utils.media_router import MediaRouter
from clichefactory._engine.parsers.text_parser import TextParser

EXTS = {".pdf", ".png", ".jpg", ".eml", ".csv", ".txt", ".docx"}


def _email(body: str = "Please find the invoice attached.") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "inbox@example.com"
    msg["Subject"] = "Invoice"
    msg.set_content(body)
    return msg


def _with_logo(msg: EmailMessage, size: int, disposition: str = "inline") -> EmailMessage:
    msg.add_alternative("<p>Regards<img src='cid:logo'></p>", subtype="html")
    msg.get_payload()[1].add_related(
        b"\x89PNG" + b"\0" * (size - 4),
        maintype="image", subtype="png", cid="<logo>", filename="image001.png",
        disposition=disposition,
    )
    return msg


def _parts(msg: EmailMessage) -> list[tuple[str, str]]:
    parsed = BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
    return [(p.filename, p.extension) for p in iter_document_parts(parsed, EXTS)]


# ---------- iter_document_parts ----------


def test_body_only_has_no_documents():
    assert _parts(_email()) == []


def test_attachments_in_order():
    msg = _email()
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="a.pdf")
    msg.add_attachment(b"x,y\n1,2", maintype="text", subtype="csv", filename="b.csv")
    assert _parts(msg) == [("a.pdf", ".pdf"), ("b.csv", ".csv")]


def test_unregistered_and_nameless_attachments_are_skipped():
    msg = _email()
    msg.add_attachment(b"zip", maintype="application", subtype="zip", filename="x.zip")
    msg.add_attachment(b"ics", maintype="text", subtype="calendar", filename="invite.ics")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf")  # no filename
    msg.add_attachment(b"", maintype="application", subtype="pdf", filename="empty.pdf")
    assert _parts(msg) == []


def test_small_inline_logo_is_skipped():
    assert _parts(_with_logo(_email(), size=5_000)) == []


def test_small_cid_image_marked_attachment_is_skipped_too():
    assert _parts(_with_logo(_email(), size=5_000, disposition="attachment")) == []


def test_large_inline_image_is_a_document():
    # A screenshot pasted into the body: inline with a Content-ID, but big.
    assert _parts(_with_logo(_email(), size=INLINE_IMAGE_MAX_BYTES + 1)) == [("image001.png", ".png")]


def test_image_attachment_without_content_id_is_a_document():
    msg = _email()
    msg.add_attachment(b"\x89PNG" + b"\0" * 100, maintype="image", subtype="png", filename="scan.png")
    assert _parts(msg) == [("scan.png", ".png")]


@pytest.mark.parametrize("filename, expected", [
    ("fwd.eml", "fwd.eml"),
    (None, "forwarded.eml"),
    ("Re: invoice", "Re: invoice.eml"),
])
def test_forwarded_email_is_one_document_not_walked_into(filename, expected):
    inner = _email("Original message.")
    inner.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="inner.pdf")
    outer = _email("See below.")
    outer.add_attachment(inner, **({"filename": filename} if filename else {}))
    parts = _parts(outer)
    assert parts == [(expected, ".eml")]  # not also inner.pdf


def test_forwarded_email_bytes_are_the_inner_message():
    inner = _email("Original message.")
    inner.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="inner.pdf")
    outer = _email("See below.")
    outer.add_attachment(inner)
    parsed = BytesParser(policy=policy.default).parsebytes(outer.as_bytes())
    (fwd,) = iter_document_parts(parsed, EXTS)
    again = BytesParser(policy=policy.default).parsebytes(fwd.content)
    assert again["Subject"] == "Invoice"
    assert [p.filename for p in iter_document_parts(again, EXTS)] == ["inner.pdf"]


def test_forwarded_email_skipped_when_eml_not_registered():
    inner = _email("Original.")
    outer = _email("See below.")
    outer.add_attachment(inner, filename="fwd.eml")
    parsed = BytesParser(policy=policy.default).parsebytes(outer.as_bytes())
    assert list(iter_document_parts(parsed, {".pdf"})) == []


def test_eml_attached_as_plain_file_is_a_document():
    inner = _email("Original.")
    outer = _email("Attached as a file.")
    outer.add_attachment(inner.as_bytes(), maintype="application", subtype="octet-stream",
                         filename="original.eml")
    assert _parts(outer) == [("original.eml", ".eml")]


def test_extension_match_is_case_insensitive():
    msg = _email()
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="INVOICE.PDF")
    parsed = BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
    assert [p.extension for p in iter_document_parts(parsed, {".PDF"})] == [".pdf"]


# ---------- EmlParser reads each document once ----------


class _RecordingTextParser(TextParser):
    calls: list[str] = []

    def document_parse(self, content: bytes, filename: str):
        _RecordingTextParser.calls.append(filename)
        return super().document_parse(content, filename)


@pytest.fixture
def parse_email():
    registry = MediaParserRegistry()
    registry.register(".txt", _RecordingTextParser)
    registry.register(".eml", EmlParser)
    router = MediaRouter(registry)
    _RecordingTextParser.calls = []

    def run(msg: EmailMessage):
        doc = EmlParser(router=router).document_parse(msg.as_bytes(), "mail.eml")
        return doc.get_markdown(), list(_RecordingTextParser.calls)

    return run


def _forward(filename: str | None) -> EmailMessage:
    inner = _email("FORWARDED-BODY: the original order.")
    inner.add_attachment("NOTES-TEXT inside the forward".encode(), maintype="text",
                         subtype="plain", filename="notes.txt")
    outer = _email("OUTER-BODY: see the forwarded email.")
    outer.add_attachment(inner, **({"filename": filename} if filename else {}))
    return outer


@pytest.mark.parametrize("filename", ["fwd.eml", None])
def test_forwarded_attachment_parsed_once_and_forwarded_body_read(parse_email, filename):
    markdown, calls = parse_email(_forward(filename))
    assert calls == ["notes.txt"]  # was 2 calls when the forward had a filename
    assert markdown.count("NOTES-TEXT") == 1
    assert "OUTER-BODY" in markdown
    assert "FORWARDED-BODY" in markdown  # was lost when the forward had no filename


def test_outer_body_is_not_taken_from_a_forwarded_email(parse_email):
    # Outer email with HTML only; the forwarded email has text/plain. The outer
    # body must come from the outer HTML, not the forwarded text.
    inner = _email("FORWARDED-BODY only.")
    outer = EmailMessage()
    outer["Subject"] = "HTML only"
    outer.set_content("<p>OUTER-HTML-BODY</p>", subtype="html")
    outer.add_attachment(inner)
    markdown, _ = parse_email(outer)
    assert markdown.count("FORWARDED-BODY") == 1
    assert "OUTER-HTML-BODY" in markdown


def test_attachment_parse_failure_skips_only_that_attachment(parse_email, monkeypatch):
    msg = _email("Body.")
    msg.add_attachment(b"good", maintype="text", subtype="plain", filename="a.txt")
    msg.add_attachment(b"bad", maintype="text", subtype="plain", filename="b.txt")
    msg.add_attachment(b"good too", maintype="text", subtype="plain", filename="c.txt")
    original = _RecordingTextParser.document_parse

    def flaky(self, content, filename):
        if filename == "b.txt":
            raise RuntimeError("boom")
        return original(self, content, filename)

    monkeypatch.setattr(_RecordingTextParser, "document_parse", flaky)
    markdown, calls = parse_email(msg)
    assert calls == ["a.txt", "c.txt"]
