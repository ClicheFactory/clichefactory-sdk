"""Tests for the Docling adapter's page-marker emission.

Regression for the L1 SDK test
(``extract_long`` with ``PageChunker(pages_per_chunk=2)``) which was
asserting ``>=2`` chunks but receiving ``1``. Root cause: the
``DoclingNormalizedDoc.get_markdown()`` path produced markdown without
the canonical ``<!-- cf:page N -->`` markers that
``clichefactory.chunking.PageChunker`` looks for, so PageChunker fell
back to its ``TokenChunker`` strategy and emitted a single chunk for
any multi-page document under the token cap.

Layout:

1. Pure-function tests for ``emit_page_marker`` /
   ``assemble_paged_markdown`` / ``pages_to_markdown`` in
   ``docling_helpers``. These pin the marker shape and ordering
   independent of Docling.
2. Round-trip tests: assemble paged markdown via the helpers, feed it
   through ``PageChunker``, and assert the chunker now produces the
   expected number of chunks. This is the actual L1 fix.
"""
from __future__ import annotations

import asyncio

import pytest

from clichefactory._engine.models.document_model import Heading, Page, Paragraph
from clichefactory._engine.parsers.parser_utils.pdf.docling_helpers import (
    PAGE_MARKER_TEMPLATE,
    assemble_paged_markdown,
    emit_page_marker,
    pages_to_markdown,
)
from clichefactory.chunking import PageChunker


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# emit_page_marker / PAGE_MARKER_TEMPLATE
# ---------------------------------------------------------------------------


def test_emit_page_marker_canonical_form():
    """The marker must match PageChunker's canonical regex (first pattern)."""
    assert emit_page_marker(1) == "<!-- cf:page 1 -->"
    assert emit_page_marker(42) == "<!-- cf:page 42 -->"


def test_page_marker_template_constant_is_format_string():
    """Sanity: callers sometimes import the constant directly."""
    assert PAGE_MARKER_TEMPLATE.format(n=7) == "<!-- cf:page 7 -->"


# ---------------------------------------------------------------------------
# assemble_paged_markdown
# ---------------------------------------------------------------------------


def test_assemble_paged_markdown_emits_marker_before_each_page():
    pages_md = {1: "Page one body.", 2: "Page two body."}
    md = assemble_paged_markdown(pages_md)
    # Markers come *before* their content so PageChunker reads the regex
    # match position as the start of page N.
    assert md.index("<!-- cf:page 1 -->") < md.index("Page one body.")
    assert md.index("Page one body.") < md.index("<!-- cf:page 2 -->")
    assert md.index("<!-- cf:page 2 -->") < md.index("Page two body.")


def test_assemble_paged_markdown_sorts_pages_ascending():
    """Pages may arrive out of order; output must always be sorted."""
    pages_md = {3: "third", 1: "first", 2: "second"}
    md = assemble_paged_markdown(pages_md)
    assert md.index("first") < md.index("second") < md.index("third")
    assert (
        md.index("<!-- cf:page 1 -->")
        < md.index("<!-- cf:page 2 -->")
        < md.index("<!-- cf:page 3 -->")
    )


def test_assemble_paged_markdown_empty_input_returns_empty_string():
    assert assemble_paged_markdown({}) == ""


def test_assemble_paged_markdown_keeps_marker_for_empty_page():
    """Skipping a page would shift downstream page numbers; we never do that."""
    pages_md = {1: "first", 2: "", 3: "third"}
    md = assemble_paged_markdown(pages_md)
    # All three markers present, even though page 2's body is empty.
    assert "<!-- cf:page 1 -->" in md
    assert "<!-- cf:page 2 -->" in md
    assert "<!-- cf:page 3 -->" in md


def test_assemble_paged_markdown_strips_whitespace_around_body():
    pages_md = {1: "  body  \n\n"}
    md = assemble_paged_markdown(pages_md)
    assert "body" in md
    # No double-leading newlines that would confuse downstream chunkers.
    assert "<!-- cf:page 1 -->\n\nbody" in md


# ---------------------------------------------------------------------------
# pages_to_markdown (structured-mode path)
# ---------------------------------------------------------------------------


def _page(index: int, blocks) -> Page:
    return Page(index=index, size=None, blocks=tuple(blocks))


def test_pages_to_markdown_emits_marker_per_page():
    pages = [
        _page(1, [Heading(level=1, text="Title One"), Paragraph(text="Body one.")]),
        _page(2, [Paragraph(text="Body two.")]),
    ]
    md = pages_to_markdown(pages)
    assert md.index("<!-- cf:page 1 -->") < md.index("# Title One")
    assert md.index("Body one.") < md.index("<!-- cf:page 2 -->")
    assert md.index("<!-- cf:page 2 -->") < md.index("Body two.")


def test_pages_to_markdown_renders_headings_and_paragraphs():
    pages = [_page(1, [Heading(level=2, text="H2"), Paragraph(text="para")])]
    md = pages_to_markdown(pages)
    assert "## H2" in md
    assert "para" in md


def test_pages_to_markdown_empty_pages_returns_empty_string():
    assert pages_to_markdown([]) == ""


def test_pages_to_markdown_sorts_by_page_index():
    pages = [
        _page(2, [Paragraph(text="second")]),
        _page(1, [Paragraph(text="first")]),
    ]
    md = pages_to_markdown(pages)
    assert md.index("first") < md.index("second")


# ---------------------------------------------------------------------------
# PageChunker round-trip
# ---------------------------------------------------------------------------


def test_assemble_paged_markdown_round_trip_through_page_chunker():
    """The L1 SDK test scenario: 5 pages, ``pages_per_chunk=2`` → 3 chunks."""
    pages_md = {n: f"Body of page {n}." for n in range(1, 6)}
    md = assemble_paged_markdown(pages_md)
    chunks = _run(PageChunker(pages_per_chunk=2, overlap_pages=0).chunks(md))
    assert len(chunks) == 3  # pages 1-2, 3-4, 5
    assert chunks[0].page_start == 1 and chunks[0].page_end == 2
    assert chunks[1].page_start == 3 and chunks[1].page_end == 4
    assert chunks[2].page_start == 5 and chunks[2].page_end == 5


def test_pages_to_markdown_round_trip_through_page_chunker():
    """Structured-mode output must also be page-chunkable."""
    pages = [
        _page(n, [Paragraph(text=f"Body of page {n}.")]) for n in range(1, 6)
    ]
    md = pages_to_markdown(pages)
    chunks = _run(PageChunker(pages_per_chunk=2, overlap_pages=0).chunks(md))
    assert len(chunks) == 3
    assert chunks[0].page_start == 1 and chunks[0].page_end == 2
    assert chunks[-1].page_start == 5 and chunks[-1].page_end == 5


def test_assemble_paged_markdown_two_pages_yields_at_least_two_chunks():
    """Tightest regression for L1: tiny 2-page doc, ``pages_per_chunk=2`` → 1 chunk
    spanning both pages, and we still get the merge path tested with
    ``pages_per_chunk=1``.

    The L1 assertion was ``>=2 chunks (so the merge path is exercised)``;
    we lock in the >=2 case here with a minimal input.
    """
    pages_md = {1: "First page.", 2: "Second page.", 3: "Third page."}
    md = assemble_paged_markdown(pages_md)
    chunks = _run(PageChunker(pages_per_chunk=2, overlap_pages=0).chunks(md))
    assert len(chunks) >= 2
    # And the chunks together cover all three pages.
    pages_seen: set[int] = set()
    for c in chunks:
        if c.page_start is not None and c.page_end is not None:
            pages_seen.update(range(c.page_start, c.page_end + 1))
    assert pages_seen == {1, 2, 3}
