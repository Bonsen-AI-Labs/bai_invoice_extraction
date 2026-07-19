"""Parsing — DI raw output into structured, per-invoice form.

`parse` turns the flat DI layout line list into grouped blocks and keeps the
prebuilt-invoice documents. `segment` splits one file into N logical invoices,
using DI's own document boundaries as the authority and title recurrence as a
sanity check. Ambiguous boundaries raise SegmentationError (pipeline parks it).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from src.config import BLOCK_GAP_FACTOR, INVOICE_TITLE_ANCHORS, MIN_X_OVERLAP
from src.services.ocr import DIResult
from src.utils.render import poly_bbox
from src.utils.text import fuzzy_eq


class SegmentationError(Exception):
    """Invoice boundaries could not be resolved confidently."""


@dataclass
class Line:
    text: str
    page: int
    polygon: list[float]  # inches

    @property
    def bbox(self):  # (min_x, min_y, max_x, max_y)
        return poly_bbox(self.polygon)


@dataclass
class Block:
    text: str
    page: int
    polygon: list[float]
    lines: list[Line] = field(default_factory=list)


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    unit: str
    angle: float


@dataclass
class ParsedDocument:
    pages: list[PageInfo]
    lines: list[Line]
    blocks: list[Block]
    tables: list[Any]
    di: DIResult


@dataclass
class LogicalInvoice:
    index: int  # 1-based ordinal within the file
    page_range: list[int]  # [first, last]
    di_document: Any | None
    lines: list[Line]
    blocks: list[Block]
    tables: list[Any]
    parsed: ParsedDocument

    @property
    def full_text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


def _line_polygon(poly) -> list[float]:
    return list(poly) if poly else [0, 0, 0, 0, 0, 0, 0, 0]


def parse(di: DIResult) -> ParsedDocument:
    pages, lines = [], []
    for p in di.layout.pages or []:
        pages.append(
            PageInfo(
                p.page_number,
                p.width or 8.5,
                p.height or 11.0,
                p.unit or "inch",
                p.angle or 0.0,
            )
        )
        for ln in p.lines or []:
            lines.append(
                Line(ln.content or "", p.page_number, _line_polygon(ln.polygon))
            )
    blocks = _group_blocks(lines)
    return ParsedDocument(
        pages=pages,
        lines=lines,
        blocks=blocks,
        tables=list(di.layout.tables or []),
        di=di,
    )


def _group_blocks(lines: list[Line]) -> list[Block]:
    """Whitespace-based block grouping in reading order."""
    blocks: list[Block] = []
    by_page: dict[int, list[Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page, []).append(ln)

    for page, plines in by_page.items():
        plines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        heights = [ln.bbox[3] - ln.bbox[1] for ln in plines] or [0.1]
        h_med = statistics.median(heights) or 0.1
        cur: list[Line] = []
        for ln in plines:
            if cur:
                prev = cur[-1]
                gap = ln.bbox[1] - prev.bbox[3]
                overlap = _x_overlap(prev.bbox, ln.bbox)
                if gap > BLOCK_GAP_FACTOR * h_med or overlap < MIN_X_OVERLAP:
                    blocks.append(_mk_block(cur))
                    cur = []
            cur.append(ln)
        if cur:
            blocks.append(_mk_block(cur))
    return blocks


def _x_overlap(a, b) -> float:
    inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    width = min(a[2] - a[0], b[2] - b[0]) or 1e-6
    return inter / width


def _mk_block(lines: list[Line]) -> Block:
    xs = [ln.bbox[0] for ln in lines] + [ln.bbox[2] for ln in lines]
    ys = [ln.bbox[1] for ln in lines] + [ln.bbox[3] for ln in lines]
    poly = [min(xs), min(ys), max(xs), min(ys), max(xs), max(ys), min(xs), max(ys)]
    return Block(
        text=", ".join(ln.text for ln in lines),
        page=lines[0].page,
        polygon=poly,
        lines=list(lines),
    )


def _doc_page_range(doc) -> list[int] | None:
    regions = getattr(doc, "bounding_regions", None) or []
    pages = [r.page_number for r in regions if getattr(r, "page_number", None)]
    return [min(pages), max(pages)] if pages else None


def segment(parsed: ParsedDocument) -> list[LogicalInvoice]:
    docs = list(parsed.di.invoice.documents or [])
    all_pages = [p.number for p in parsed.pages] or [1]
    full_range = [min(all_pages), max(all_pages)]
    title_count = sum(
        1
        for ln in parsed.lines
        if any(fuzzy_eq(ln.text, t) for t in INVOICE_TITLE_ANCHORS)
    )

    if len(docs) <= 1:
        if not docs and title_count > 1:
            raise SegmentationError(f"{title_count} invoice titles, no DI documents")
        return [_slice(parsed, 1, full_range, docs[0] if docs else None)]

    invoices = []
    for i, doc in enumerate(docs, 1):
        pr = _doc_page_range(doc) or full_range
        invoices.append(_slice(parsed, i, pr, doc))
    return invoices


def _slice(
    parsed: ParsedDocument, index: int, page_range: list[int], doc
) -> LogicalInvoice:
    lo, hi = page_range
    def in_range(pg):
        return lo <= pg <= hi
    lines = [ln for ln in parsed.lines if in_range(ln.page)]
    blocks = [b for b in parsed.blocks if in_range(b.page)]
    tables = (
        [
            t
            for t in parsed.tables
            if any(in_range(r.page_number) for r in (t.bounding_regions or []))
        ]
        if parsed.tables
        else []
    )
    return LogicalInvoice(
        index=index,
        page_range=page_range,
        di_document=doc,
        lines=lines,
        blocks=blocks,
        tables=tables,
        parsed=parsed,
    )
