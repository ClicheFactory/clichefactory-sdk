from collections import defaultdict
from typing import DefaultDict, Dict, Literal, Optional, Sequence, Tuple

from clichefactory._engine.models.normalized_doc import NormalizedDoc
from clichefactory._engine.models.document_model import BBox, Block, Heading, Image, Page
from clichefactory._engine.models.document_model import Paragraph, Section, Table, TableCell
from docling_core.types.doc.base import BoundingBox
from docling_core.types.doc.document import (
    DocItem,
    DoclingDocument,
    PictureItem,
    SectionHeaderItem,
    TableItem,
    NodeItem,
    TextItem,
    TitleItem,
    ListItem,
    CodeItem,
    FormulaItem,
    KeyValueItem
)


class DoclingNormalizedDoc(NormalizedDoc):
    """
    NormalizedDoc from Docling document.

    output_mode:
        "markdown" — assembles per-page markdown from Docling's
        ``iterate_items`` walk (via ``build_per_page_markdown``) and
        joins it with canonical ``<!-- cf:page N -->`` markers. Falls
        back to ``docling_document.export_to_markdown()`` only when no
        per-page content can be recovered (e.g. some DOCX docs report
        as a single synthetic page).
        "structured" — walks our own ``Page``/``Block`` model and emits
        page markers between pages via ``pages_to_markdown``.

    Both modes produce output that ``clichefactory.chunking.PageChunker``
    can split on page boundaries; before this, both went through
    ``DoclingDocument.export_to_markdown`` / a section-walk that
    omitted page markers, so ``PageChunker(pages_per_chunk=N)`` always
    fell back to ``TokenChunker`` and produced a single chunk for any
    multi-page document under the token cap.
    """

    def __init__(
        self,
        docling_document: DoclingDocument,
        *,
        output_mode: Literal["markdown", "structured"] = "markdown",
    ) -> None:
        self.docling_document = docling_document
        self._output_mode = output_mode
        self.pages: Sequence[Page] = self.build_pages()
        self.sections: Sequence[Section] = self.build_sections()

        self.images = tuple(
            block
            for page in self.pages
            for block in page.blocks
            if isinstance(block, Image)
        )

        self.tables = tuple(
            block
            for page in self.pages
            for block in page.blocks
            if isinstance(block, Table)
        )

    # ----- Implementation of the NormalizedDoc interface -----

    def get_plain_text(self) -> str:
        return self.docling_document.export_to_text()

    def get_markdown(self) -> str:
        from clichefactory._engine.parsers.parser_utils.pdf.docling_helpers import (
            assemble_paged_markdown,
            build_per_page_markdown,
            pages_to_markdown,
        )

        if self._output_mode == "markdown":
            pages_md = build_per_page_markdown(self.docling_document)
            if pages_md:
                return assemble_paged_markdown(pages_md)
            # No item carried per-page provenance — happens for some
            # synthetic single-page DOCX outputs. Fall back to docling's
            # own markdown export so we still return *something* useful;
            # PageChunker will degrade to its TokenChunker fallback in
            # this case, which is the pre-fix behaviour.
            return self.docling_document.export_to_markdown()

        return pages_to_markdown(self.pages)
    
    # ----- Docling to document_model mapping functions -----

    def build_pages(self) -> Sequence[Page]:
        blocks_by_page = self._collect_blocks_by_page()

        # If docling provides pages, respect them
        if self.docling_document.pages:
            pages: list[Page] = []
            for index, page_item in self.docling_document.pages.items():
                size_tuple = (page_item.size.width, page_item.size.height) if page_item.size else None
                page_blocks = tuple(blocks_by_page.get(index, []))
                pages.append(Page(index=index, size=size_tuple, blocks=page_blocks))
            return tuple(pages)

        # Otherwise: single synthetic page (DOCX pagination may be unavailable)
        all_blocks: list[Block] = []
        for page_idx in sorted(blocks_by_page.keys()):
            all_blocks.extend(blocks_by_page[page_idx])
        if not all_blocks:
            # last resort: collect in reading order with no page assignment
            for item, _ in self.docling_document.iterate_items(with_groups=False):
                b = self._to_block(item)
                if b is not None:
                    all_blocks.append(b)

        return (Page(index=1, size=None, blocks=tuple(all_blocks)),)

 
    def build_sections(self) -> Sequence[Section]:
        """
        Build a semantic hierarchy from headings.

        Strategy:
        - Flatten blocks in reading order (page order, then block order).
        - Start a new section when we hit a Heading.
        - Nest based on heading level (H1 contains H2, etc.)
        """
        flat_blocks: list[Block] = []
        # reading order: by page index then appearance
        for page in sorted(self.pages, key=lambda p: p.index):
            flat_blocks.extend(page.blocks)

        # Collect top-level sections using a stack of (level, SectionBuilder)
        class _Builder:
            def __init__(self, heading: Heading) -> None:
                self.heading = heading
                self.blocks: list[Block] = []
                self.children: list[Section] = []

            def finalize(self) -> Section:
                return Section(heading=self.heading, blocks=tuple(self.blocks), subsections=tuple(self.children))

        stack: list[Tuple[int, _Builder]] = []
        roots: list[Section] = []

        def push_section(h: Heading) -> None:
            nonlocal stack, roots
            b = _Builder(h)
            lvl = max(1, int(h.level))

            # pop until parent is strictly lower level
            while stack and stack[-1][0] >= lvl:
                closed_lvl, closed = stack.pop()
                sec = closed.finalize()
                if stack:
                    stack[-1][1].children.append(sec)
                else:
                    roots.append(sec)

            stack.append((lvl, b))

        def add_to_current(block: Block) -> None:
            if not stack:
                # No heading seen yet: create an implicit H1 section so content isn’t lost
                push_section(Heading(level=1, text="Document"))
            stack[-1][1].blocks.append(block)

        for blk in flat_blocks:
            if isinstance(blk, Heading):
                push_section(blk)
            else:
                add_to_current(blk)

        # close remaining
        while stack:
            lvl, b = stack.pop()
            sec = b.finalize()
            if stack:
                stack[-1][1].children.append(sec)
            else:
                roots.append(sec)

        return tuple(roots)

    # Collect all blocks for all pages (poor man's provenance))
    def _collect_blocks_by_page(self) -> Dict[int, list[Block]]:
        blocks: DefaultDict[int, list[Block]] = defaultdict(list)
        for item, _ in self.docling_document.iterate_items(with_groups=False):
            block = self._to_block(item)
            
            if block is None:
                continue

            if not isinstance(item, DocItem):  # Could have been a GroupItem or NodeItem or similar semantic, non content descriptor
                continue

            page_no = self._get_primary_page(item)
            if page_no is None:
                continue

            blocks[page_no].append(block)

        return blocks


    def _to_block(self, item: NodeItem) -> Optional[Block]:
        if not isinstance(item, DocItem):
            return None

        bbox = self._item_bbox(item)

        if isinstance(item, TableItem):
            return self._table_to_block(item, bbox)
        if isinstance(item, PictureItem):
            return self._image_to_block(item, bbox)
        if isinstance(item, SectionHeaderItem):
            return Heading(level=item.level, text=item.text, bbox=bbox)
        if isinstance(item, TitleItem):
            return Heading(level=1, text=item.text, bbox=bbox)
        if isinstance(item, TextItem):
           return Paragraph(text=item.text, bbox=bbox)
        if isinstance(item, ListItem):
           return Paragraph(text=item.text, bbox=bbox)
        if isinstance(item, CodeItem):
           return Paragraph(text=item.text, bbox=bbox)
        if isinstance(item, FormulaItem):
           return Paragraph(text=item.text, bbox=bbox)
        if isinstance(item, KeyValueItem):
           return Paragraph(text=self._key_value_to_md(item), bbox=bbox)
        # if isinstance(item, FormItem):
        # Made of KeyValueItems, TextItems, etc., skipping for now
        
        return None

    def _table_to_block(self, table_item: TableItem, bbox: Optional[BBox]) -> Optional[Table]:
        cells: list[TableCell] = []
        page_height = self._get_page_height(table_item)

        for cell in table_item.data.table_cells:
            cell_bbox = self._normalize_bbox(cell.bbox, page_height)
            cells.append(
                TableCell(
                    text=cell.text,
                    row=cell.start_row_offset_idx,
                    col=cell.start_col_offset_idx,
                    bbox=cell_bbox,
                )
            )

        return Table(cells=tuple(cells), bbox=bbox)

    def _image_to_block(self, picture_item: PictureItem, bbox: Optional[BBox]) -> Optional[Image]:
        img_ref = picture_item.image
        if img_ref is None:
            ref = picture_item.self_ref
            mime = "application/octet-stream"
        else:
            ref = str(img_ref.uri)
            mime = img_ref.mimetype

        alt_text = picture_item.caption_text(self.docling_document) or None
        return Image(ref=ref, mime_type=mime, bbox=bbox, alt_text=alt_text)

    def _get_primary_page(self, item: DocItem) -> Optional[int]:
        if not item.prov:
            return None
        return item.prov[0].page_no

    def _item_bbox(self, item: DocItem) -> Optional[BBox]:
        if not item.prov:
            return None
        return self._normalize_bbox(item.prov[0].bbox, self._get_page_height(item))

    def _normalize_bbox(self, bbox: Optional[BoundingBox], page_height: Optional[float]) -> Optional[BBox]:
        if bbox is None:
            return None
        if page_height:
            return bbox.to_top_left_origin(page_height).as_tuple()
        return bbox.as_tuple()

    def _get_page_height(self, item: DocItem) -> Optional[float]:
        page_no = self._get_primary_page(item)
        if page_no is None:
            return None
        page = self.docling_document.pages.get(page_no)
        if page is None:
            return None
        return page.size.height
    
    def _key_value_to_md(self, item: KeyValueItem) -> str:
        # Safely obtain the dumped mapping, then get the first key and its value without indexing dict_keys
        key = ""
        value = ""
        dumped_kv = item.model_dump() if hasattr(item, "model_dump") else {}
        key = next(iter(dumped_kv.keys()), "") or ""
        value = dumped_kv.get(key, "")

        key = (key or "").strip()
        value = (str(value) if value is not None else "").strip()

        return f"{key}: {value}"

