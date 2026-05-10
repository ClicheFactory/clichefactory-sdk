"""
Shared helpers for Docling-based PDF parsers.

Used by docling_baseline and docling_vlm strategies.
"""
from __future__ import annotations

import io
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Sequence

from docling_core.types.doc.base import BoundingBox
from docling_core.types.doc.document import (
    DocItem,
    KeyValueItem,
    ListItem,
    PictureItem,
    SectionHeaderItem,
    TableItem,
    TitleItem,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from clichefactory._engine.models.document_model import Block, Heading, Image, Page, Section, Table
    from PIL import Image as PILImage


# ── Page markers ──────────────────────────────────────────────────────────
# Read by ``clichefactory.chunking.PageChunker._PAGE_MARKER_PATTERNS``.
# We emit the ``<!-- cf:page N -->`` form (vs. the legacy ``<!-- page: N -->``)
# because the canonical pattern is listed first in PageChunker's matcher
# and is what new code is expected to produce.

PAGE_MARKER_TEMPLATE = "<!-- cf:page {n} -->"


def emit_page_marker(page_no: int) -> str:
    """Return the canonical ClicheFactory page marker for ``page_no``."""
    return PAGE_MARKER_TEMPLATE.format(n=page_no)


def assemble_paged_markdown(pages_md: dict[int, str]) -> str:
    """Concatenate per-page markdown bodies with canonical page markers.

    Each page's body is preceded by its marker (not followed by it), so
    ``PageChunker`` assigns the correct ``page_start`` to each chunk:
    the regex match position is the start of page ``N``, and content
    *after* it belongs to that page.

    Empty bodies are still preceded by their marker — the page exists
    in the source document and skipping it would shift downstream page
    numbers. Pages are emitted in ascending order regardless of dict
    iteration order.

    Returns ``""`` for an empty input dict.
    """
    if not pages_md:
        return ""
    parts: list[str] = []
    for page_no in sorted(pages_md.keys()):
        parts.append(emit_page_marker(page_no))
        body = (pages_md[page_no] or "").strip()
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def normalize_pil_image(pil_img: "PILImage.Image") -> "PILImage.Image":
    """
    Force a PIL image into an in-memory RGB form so crop/OpenCV/save never
    hit Docling's lazy or odd internal streams.
    """
    from PIL import Image as PILImage
    from PIL import ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        pil_img.load()
    except OSError as e:
        logger.warning(
            "Pillow hit an OS error while forcing image load: %s",
            e,
        )

    buf = io.BytesIO()
    try:
        pil_img.save(buf, format="PNG")
    except Exception as e:
        logger.error(
            "Failed to save image to buffer; falling back to blank RGB: %s",
            e,
        )
        size = getattr(pil_img, "size", None) or (100, 100)
        pil_img = PILImage.new("RGB", size, (255, 255, 255))
        pil_img.save(buf, format="PNG")

    buf.seek(0)
    out = PILImage.open(buf).copy()
    return out.convert("RGB")


def page_to_pil(page) -> "PILImage.Image | None":
    """Get PIL Image from Docling page.image."""
    img = getattr(page, "image", None)
    if img is None:
        return None
    if hasattr(img, "pil_image"):
        raw = img.pil_image
    else:
        raw = img
    if raw is None:
        return None
    try:
        return normalize_pil_image(raw)
    except Exception as e:
        logger.warning("Failed to normalize page image (page %s): %s", getattr(page, "page_no", "?"), e)
        return None


def normalize_bbox(bbox: BoundingBox | None, page_height: float) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    return bbox.to_top_left_origin(page_height).as_tuple()


def item_to_text(item: DocItem) -> str:
    """Get plain text or markdown snippet for a DocItem."""
    text = getattr(item, "text", None) or ""
    text = (text or "").strip()
    if isinstance(item, SectionHeaderItem):
        level = getattr(item, "level", 1) or 1
        level = max(1, min(6, int(level)))
        return "#" * level + " " + text if text else ""
    if isinstance(item, TitleItem):
        return "# " + text if text else ""
    if isinstance(item, ListItem):
        return "- " + text if text else ""
    if isinstance(item, KeyValueItem):
        dumped = item.model_dump() if hasattr(item, "model_dump") else {}
        key = next(iter(dumped.keys()), "") or ""
        value = dumped.get(key, "")
        key = (key or "").strip()
        value = (str(value) if value is not None else "").strip()
        return f"{key}: {value}" if key else text
    return text


def table_to_markdown(table_item: TableItem) -> str:
    """Format Docling TableItem as a simple Markdown table from table_cells."""
    if not getattr(table_item, "data", None) or not getattr(table_item.data, "table_cells", None):
        return ""
    cells = table_item.data.table_cells
    if not cells:
        return ""
    rows: dict[int, dict[int, str]] = {}
    for c in cells:
        r = getattr(c, "start_row_offset_idx", 0)
        col = getattr(c, "start_col_offset_idx", 0)
        text = getattr(c, "text", "") or ""
        if r not in rows:
            rows[r] = {}
        rows[r][col] = text
    if not rows:
        return ""
    max_row = max(rows.keys())
    max_col = max(max(rows[r].keys()) for r in rows)
    lines: list[str] = []
    for r in range(max_row + 1):
        row_cells = rows.get(r, {})
        line = "| " + " | ".join((row_cells.get(c, "").replace("|", "\\|") for c in range(max_col + 1))) + " |"
        lines.append(line)
        if r == 0:
            lines.append("|" + "---|" * (max_col + 1))
    return "\n".join(lines)


def item_to_markdown(item: DocItem) -> str:
    """Get markdown for one item (text or table)."""
    if isinstance(item, TableItem):
        return table_to_markdown(item)
    return item_to_text(item)


def build_per_page_markdown(doc) -> dict[int, str]:
    """Build markdown per page from Docling items in reading order."""
    page_parts: dict[int, list[str]] = defaultdict(list)
    for item, _ in doc.iterate_items(with_groups=False):
        if not isinstance(item, DocItem):
            continue
        if not getattr(item, "prov", None) or len(item.prov) == 0:
            continue
        page_no = item.prov[0].page_no
        md = item_to_markdown(item)
        if md:
            page_parts[page_no].append(md)
    return {pno: "\n\n".join(parts) for pno, parts in sorted(page_parts.items())}


def body_snippet_around_placeholder(
    body_md: str,
    placeholder: str,
    *,
    window: int = 20,
) -> str:
    """Return a small window of lines around the first occurrence of placeholder."""
    if not body_md or not placeholder:
        return body_md or ""
    lines = body_md.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if placeholder in line:
            idx = i
            break
    if idx is None:
        return body_md
    start = max(0, idx - window)
    end = min(len(lines), idx + window + 1)
    return "\n".join(lines[start:end])


def build_reading_order_index(
    doc,
) -> tuple[list[tuple[DocItem, int | None]], dict[int, int], dict[int, int]]:
    """Build reading-order index over Docling DocItems."""
    reading_order: list[tuple[DocItem, int | None]] = []
    table_index_map: dict[int, int] = {}
    figure_index_map: dict[int, int] = {}
    for item, _ in doc.iterate_items(with_groups=False):
        if not isinstance(item, DocItem):
            continue
        page_no: int | None = None
        prov = getattr(item, "prov", None)
        if prov and len(prov) > 0:
            page_no = getattr(prov[0], "page_no", None)
        idx = len(reading_order)
        reading_order.append((item, page_no))
        if isinstance(item, TableItem):
            table_index_map[id(item)] = idx
        elif isinstance(item, PictureItem):
            figure_index_map[id(item)] = idx
    return reading_order, table_index_map, figure_index_map


def extract_table_context_snippet(
    doc,
    reading_order: list[tuple[DocItem, int | None]],
    table_index_map: dict[int, int],
    table_item: TableItem,
    *,
    window: int = 3,
    max_chars: int = 400,
) -> str:
    """Extract a compact snippet of nearby non-table text for a table."""
    key = id(table_item)
    if not reading_order or key not in table_index_map:
        return ""

    table_idx = table_index_map.get(key)
    if table_idx is None or table_idx < 0 or table_idx >= len(reading_order):
        return ""

    _, table_page = reading_order[table_idx]

    def _collect_neighbours(prefer_same_page: bool) -> list[str]:
        texts: list[str] = []
        seen_indices: set[int] = set()
        for offset in range(1, window + 1):
            for j in (table_idx - offset, table_idx + offset):
                if j < 0 or j >= len(reading_order) or j in seen_indices:
                    continue
                item, page_no = reading_order[j]
                if isinstance(item, TableItem):
                    continue
                if prefer_same_page and table_page is not None and page_no != table_page:
                    continue
                txt = item_to_text(item)
                if txt:
                    texts.append(txt)
                    seen_indices.add(j)
        return texts

    parts = _collect_neighbours(prefer_same_page=True)
    if not parts:
        parts = _collect_neighbours(prefer_same_page=False)
    if not parts:
        return ""

    snippet = "\n".join(parts).strip()
    if len(snippet) <= max_chars:
        return snippet

    truncated = snippet[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."


def build_body_markdown_with_placeholders(doc) -> str:
    """Build body markdown with [TABLE_N], [FIGURE_N] placeholders."""
    parts: list[str] = []
    table_index = 0
    figure_index = 0
    for item, _ in doc.iterate_items(with_groups=False):
        if not isinstance(item, DocItem):
            continue
        if isinstance(item, TableItem):
            table_index += 1
            parts.append(f"[TABLE_{table_index}]")
        elif isinstance(item, PictureItem):
            figure_index += 1
            parts.append(f"[FIGURE_{figure_index}]")
        else:
            text = item_to_text(item)
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def collect_visual_items_in_reading_order(doc) -> list[tuple[str, int, DocItem]]:
    """Return (kind, per_kind_1based_index, item) for every table and figure."""
    items: list[tuple[str, int, DocItem]] = []
    table_idx = 0
    figure_idx = 0
    for item, _ in doc.iterate_items(with_groups=False):
        if not isinstance(item, DocItem):
            continue
        if isinstance(item, TableItem):
            table_idx += 1
            items.append(("table", table_idx, item))
        elif isinstance(item, PictureItem):
            figure_idx += 1
            items.append(("figure", figure_idx, item))
    return items


def item_prov_to_crops(doc, item: DocItem) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Get (page_no, (x0,y0,x1,y1)) for each prov of item."""
    out: list[tuple[int, tuple[float, float, float, float]]] = []
    prov = getattr(item, "prov", None)
    if not prov:
        return out
    for p in prov:
        if p.bbox is None:
            continue
        page_no = p.page_no
        page = doc.pages.get(page_no)
        if page is None or page.size is None:
            continue
        page_height = float(page.size.height)
        bbox_tuple = normalize_bbox(p.bbox, page_height)
        if bbox_tuple is not None:
            out.append((page_no, bbox_tuple))
    return out


def first_item_bbox_for_prompt(doc, item: DocItem) -> tuple[float, float, float, float] | None:
    """Get (x, y, width, height) for first prov of item."""
    crops = item_prov_to_crops(doc, item)
    if not crops:
        return None
    _page_no, (x0, y0, x1, y1) = crops[0]
    return (x0, y0, x1 - x0, y1 - y0)


def crop_region_to_png_bytes(
    pil_img: "PILImage.Image",
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    page_width: float,
    page_height: float,
    padding: int = 20,
    upscale_factor: int = 2,
    opencv_enhance: bool = False,
) -> bytes:
    """Crop region from page image to PNG bytes."""
    try:
        pil_img = normalize_pil_image(pil_img)
    except Exception as exc:
        logger.warning("Failed to normalize image in crop_region_to_png_bytes: %s", exc)
        raise

    from PIL import Image as PILImage

    left = max(0, int(x0) - padding)
    top = max(0, int(y0) - padding)
    right = min(int(page_width), int(x1) + padding)
    bottom = min(int(page_height), int(y1) + padding)
    crop = pil_img.crop((left, top, right, bottom))
    if upscale_factor > 1:
        new_size = (crop.width * upscale_factor, crop.height * upscale_factor)
        crop = crop.resize(new_size, resample=PILImage.Resampling.LANCZOS)

    if opencv_enhance:
        try:
            import cv2
            import numpy as np

            arr = np.array(crop)
            if len(arr.shape) == 2:
                lab = cv2.cvtColor(arr[:, :, np.newaxis], cv2.COLOR_GRAY2BGR)
                lab = cv2.cvtColor(lab, cv2.COLOR_BGR2LAB)
            else:
                lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
            l_chan, a_chan, b_chan = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            l_chan = clahe.apply(l_chan)
            enhanced_lab = cv2.merge([l_chan, a_chan, b_chan])
            enhanced_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
            enhanced_rgb = cv2.cvtColor(enhanced_bgr, cv2.COLOR_BGR2RGB)
            crop = PILImage.fromarray(enhanced_rgb)
        except Exception as exc:
            logger.warning("OpenCV enhancement failed, using raw crop: %s", exc)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _block_to_md(block: "Block") -> str:
    """Markdown for one document_model ``Block``.

    Module-level helper so both ``blocks_to_markdown`` (section-organised)
    and ``pages_to_markdown`` (page-organised, with page markers) can
    reuse the same per-block rendering.
    """
    from clichefactory._engine.models.document_model import Heading, Image, Paragraph, Table

    if isinstance(block, Heading):
        return "#" * max(1, block.level) + " " + (block.text or "")
    if isinstance(block, Paragraph):
        return block.text or ""
    if isinstance(block, Table):
        return _table_block_to_markdown(block)
    if isinstance(block, Image):
        alt = block.alt_text or "image"
        return f"![{alt}]({block.ref})"
    return ""


def _table_block_to_markdown(t: "Table") -> str:
    if not t.cells:
        return ""
    rows: dict[int, dict[int, str]] = {}
    for c in t.cells:
        if c.row not in rows:
            rows[c.row] = {}
        rows[c.row][c.col] = (c.text or "").replace("|", "\\|")
    if not rows:
        return ""
    max_row = max(rows.keys())
    max_col = max(max(rows[r].keys()) for r in rows)
    lines: list[str] = []
    for r in range(max_row + 1):
        row_cells = rows.get(r, {})
        line = "| " + " | ".join((row_cells.get(c, "") for c in range(max_col + 1))) + " |"
        lines.append(line)
        if r == 0:
            lines.append("|" + "---|" * (max_col + 1))
    return "\n".join(lines)


def blocks_to_markdown(
    pages: "Sequence[Page]",
    sections: "Sequence[Section]",
    images: "Sequence[Image]",
    tables: "Sequence[Table]",
) -> str:
    """
    Convert document_model blocks to section-organised markdown.

    Walks the semantic ``sections`` hierarchy. Does *not* emit page
    markers — page boundaries don't naturally align with heading
    boundaries, and trying to interleave them produces confusing
    output. Use ``pages_to_markdown`` instead when you need
    ``PageChunker`` to be able to split the result.
    """

    def section_to_md(sec: "Section") -> list[str]:
        out: list[str] = []
        out.append("#" * max(1, sec.heading.level) + " " + (sec.heading.text or ""))
        for block in sec.blocks:
            md = _block_to_md(block)
            if md:
                out.append(md)
        for sub in sec.subsections:
            out.extend(section_to_md(sub))
        return out

    parts: list[str] = []
    for section in sections:
        parts.extend(section_to_md(section))

    return "\n\n".join(parts)


def pages_to_markdown(pages: "Sequence[Page]") -> str:
    """Convert document_model pages to page-organised markdown with markers.

    Walks ``pages`` in ascending ``index`` order, emits the canonical
    ``<!-- cf:page N -->`` marker before each page's body, and renders
    every block on the page in source order using ``_block_to_md``.

    This is the structured-mode counterpart to ``assemble_paged_markdown``
    (which works off pre-rendered per-page markdown from Docling).
    Both produce output that ``PageChunker`` can split on page
    boundaries.
    """
    if not pages:
        return ""
    parts: list[str] = []
    for page in sorted(pages, key=lambda p: p.index):
        parts.append(emit_page_marker(page.index))
        body_parts: list[str] = []
        for block in page.blocks:
            md = _block_to_md(block)
            if md:
                body_parts.append(md)
        if body_parts:
            parts.append("\n\n".join(body_parts))
    return "\n\n".join(parts)
