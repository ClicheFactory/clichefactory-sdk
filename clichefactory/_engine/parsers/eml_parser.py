from email import policy
from email.parser import BytesParser
from email.message import Message

from typing import List
import logging

from clichefactory._engine.adapters.eml_adapter import EmlDoc
from clichefactory._engine.models.document_model import Block, Heading, Page, Paragraph, Section
from clichefactory._engine.models.normalized_doc import NormalizedDoc
from clichefactory._engine.parsers.doc_parser import DocParser
from clichefactory._engine.parsers.docx_parser import DocxParser
from clichefactory._engine.parsers.parser_utils.pdf.strategies import DoclingBaselineStrategy
from clichefactory._engine.parsers.media_parser_registry import MediaParserRegistry
from clichefactory._engine.parsers.media_parser import MediaParser
from clichefactory._engine.parsers.parser_utils.eml_utils import (
    build_markdown,
    build_plain_text,
    extract_body,
    iter_document_parts,
)
from clichefactory._engine.parsers.parser_utils.media_router import MediaRouter
from clichefactory._engine.parsers.xlsx_parser import XlsxParser
from clichefactory._engine.parsers.csv_parser import CsvParser

logger = logging.getLogger(__name__)

class EmlParser(MediaParser):
    """
    MediaParser implementation for .eml files.
    """
    def __init__(self, router=None, cacher=None, media_parser_registry=None, **kwargs):
        super().__init__(cacher=cacher, media_parser_registry=media_parser_registry, **kwargs)
        if media_parser_registry is not None:
            self.router = MediaRouter(media_parser_registry, cacher)
        elif router is not None:
            self.router = router
        else:
            parser_registry = MediaParserRegistry()
            parser_registry.register(".docx", DocxParser)
            parser_registry.register(".pdf", DoclingBaselineStrategy)
            parser_registry.register_many([".odt", ".doc"], DocParser)
            parser_registry.register(".xlsx", XlsxParser)
            parser_registry.register(".csv", CsvParser)
            self.router = MediaRouter(parser_registry, cacher)
    
    def _parse_attachments(self, msg: Message) -> list[NormalizedDoc]:
        """Parse each document in the email once (see ``iter_document_parts``)."""
        docs = []
        extensions = self.router.registry.get_registered_extensions()
        for part in iter_document_parts(msg, extensions):
            try:
                doc = self.router.parse(part.content, part.filename)
            except Exception as e:
                logger.exception("Attachment parse failed for %s: %s", part.filename, e)
                continue
            if doc is not None:
                docs.append(doc)
        return docs

    def document_parse(self, content: bytes, filename: str) -> NormalizedDoc:
        # 1. Parse the RFC 2822 message
        msg = BytesParser(policy=policy.default).parsebytes(content)

        # 2. Extract body text
        body = extract_body(msg)
        body_plain = body["text_plain"]

        # 3. Extract attachments
        normalized_attachments = self._parse_attachments(msg)

        # 4. Build plain text and markdown representations
        plain_text = build_plain_text(msg, body_plain, normalized_attachments)
        markdown = build_markdown(msg, body_plain, normalized_attachments) # TODO: Use text_html here if available

        # 5. Build a simple layout model (single page)
        subject = msg.get("Subject", "(no subject)")

        h1 = Heading(level=1, text=subject)
        header_heading = Heading(level=2, text="Headers")
        body_heading = Heading(level=2, text="Body")

        header_paragraphs: List[Paragraph] = []
        for name in ["From", "To", "Cc", "Bcc", "Date", "Message-ID"]:
            val = msg.get(name)
            if val:
                header_paragraphs.append(Paragraph(text=f"{name}: {val}"))

        body_paragraphs: List[Paragraph] = []
        if body_plain:
            for chunk in body_plain.split("\n\n"):
                chunk = chunk.strip()
                if chunk:
                    body_paragraphs.append(Paragraph(text=chunk))

        # Optional: add images as layout blocks as well: 
        # first extract images from attachments and convert the info to type Image

        # image_blocks: List[Image] = list(images)

        page_blocks: List[Block] = [h1, header_heading, *header_paragraphs,
                                    #body_heading, *body_paragraphs, *image_blocks]
                                    body_heading, *body_paragraphs]

        page = Page(index=0, size=None, blocks=page_blocks)


        # 6. Build a simple semantic model
        header_section = Section(
            heading=header_heading,
            blocks=header_paragraphs,
            subsections=[],
        )

        # Build body section with attachments as subsections
        attachment_subsections: List[Section] = []

        for attachment_doc in normalized_attachments:
            # Each attachment has its own root section(s).  
            # We wrap each attachment as a subsection under "Body".
            if attachment_doc.get_sections():
                # Use the attachment's top-level sections directly
                for root in attachment_doc.get_sections():
                    attachment_subsections.append(root)
            else:
                # Fallback: attachment with no structured sections → treat its text as a single block
                fallback_section = Section(
                    heading=Heading(level=3, text="Attachment"),
                    blocks=[Paragraph(text=attachment_doc.get_markdown())],
                    subsections=[],
                )
                attachment_subsections.append(fallback_section)

        body_sections = Section(
            heading=body_heading,
            blocks=body_paragraphs,          # the body text belongs here
            subsections=attachment_subsections,  # attachments (with their own nested structure)
        )

        root_section = Section(
            heading=h1,
            blocks=[],
            subsections=[header_section, body_sections],
        )

        # 7. Summary text: subject + first line of body as a simple heuristic
        # TODO: Implement summarization with LLM.
        summary_text = ""

        # 8. Create EmlDoc
        doc = EmlDoc(
            summary_text=summary_text,
            media_type="message/rfc822",
            pages=[page],
            sections=[root_section],
            images=(),  # You may want to add images here someday
            tables=(),  # no table extraction for now
            _plain_text=plain_text,
            _markdown=markdown,
        )

        return doc
    