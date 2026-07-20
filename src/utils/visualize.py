"""Extract all local PDFs and render annotated page images.

Bypasses SharePoint/Excel/Cosmos — reads PDFs straight from `data/invoices`,
runs the real OCR → parse → segment → extract path (LLM arbitration included),
and writes `out/<file>-<evalid>-p<n>.png` with each extracted field boxed and
labelled `field (confidence)`. Visual smoke test, writes nothing back.

Guarded to ENVIRONMENT == "local". Run: `uv run python -m src.utils.visualize`
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import fitz  # pymupdf

from src.env import Settings
from src.extraction.client import Extractor
from src.models import InvoiceRecord
from src.parsing.client import parse, segment
from src.services.llm import LLMClient
from src.services.ocr import OCRClient
from src.template_generation import JsonTemplateStore, TemplateEngine
from src.utils.logging import get_logger, setup_observability

_INVOICE_DIR = Path("data/invoices")
_OUT_DIR = Path("out")
_DPI = 150
_PT_PER_INCH = 72.0
_RED = (0.85, 0.1, 0.1)
_WHITE = (1, 1, 1)

log = get_logger("visualize")


def _annotate(pdf_bytes: bytes, rec: InvoiceRecord, stem: str) -> list[Path]:
    """Draw each resolved field's box + label on its page, save one PNG per page."""
    # Group fields by page; skip fields with no value or no geometry.
    by_page: dict[int, list[tuple[str, object, float, list[float]]]] = {}
    for fkey, fr in rec.fields.items():
        if fr.value is None or not fr.polygon or not fr.page:
            continue
        by_page.setdefault(fr.page, []).append(
            (fkey, fr.value, fr.confidence, fr.polygon)
        )

    saved: list[Path] = []
    zoom = _DPI / _PT_PER_INCH
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_no, fields in sorted(by_page.items()):
            page = doc[page_no - 1]
            for fkey, _val, conf, poly in fields:
                xs, ys = poly[0::2], poly[1::2]
                rect = fitz.Rect(
                    min(xs) * _PT_PER_INCH,
                    min(ys) * _PT_PER_INCH,
                    max(xs) * _PT_PER_INCH,
                    max(ys) * _PT_PER_INCH,
                )
                page.draw_rect(rect, color=_RED, width=1.2)
                label = f"{fkey} ({conf:.2f})"
                ly = max(rect.y0 - 3, 9)
                lx = rect.x0
                # white plate behind the label so it stays legible over the scan
                page.draw_rect(
                    fitz.Rect(lx - 1, ly - 8, lx + len(label) * 4.6 + 2, ly + 2),
                    color=_WHITE,
                    fill=_WHITE,
                )
                page.insert_text((lx, ly), label, fontsize=7, color=_RED)
            out_path = _OUT_DIR / f"{stem}-{rec.eval_id}-p{page_no}.png"
            page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(out_path)
            saved.append(out_path)
    return saved


async def run() -> None:
    settings = Settings()  # type: ignore[call-arg]  # fields come from the env
    setup_observability(settings)
    if settings.ENVIRONMENT != "local":
        log.warning(
            "visualize only runs when ENVIRONMENT=local (got %s)", settings.ENVIRONMENT
        )
        return

    _OUT_DIR.mkdir(exist_ok=True)
    pdfs = sorted(_INVOICE_DIR.glob("*.pdf"))
    log.info("visualizing %d invoices → %s/", len(pdfs), _OUT_DIR)

    ocr = OCRClient(settings)
    extractor = Extractor(LLMClient(settings), TemplateEngine(JsonTemplateStore()))
    try:
        for pdf_path in pdfs:
            try:
                pdf = pdf_path.read_bytes()
                di = ocr.analyze_invoice(pdf)  # coro
                invoices = segment(parse(await di))
                for inv in invoices:
                    eval_id = f"LOCAL-{pdf_path.stem[:12]}-{inv.index:02d}"
                    rec = await extractor.extract(inv, pdf, eval_id, str(pdf_path))
                    imgs = _annotate(pdf, rec, pdf_path.stem)
                    filled = sum(1 for f in rec.fields.values() if f.value is not None)
                    log.info(
                        "%s inv#%d: %d/%d fields, review=%s → %d img",
                        pdf_path.name,
                        inv.index,
                        filled,
                        len(rec.fields),
                        rec.needs_review,
                        len(imgs),
                    )
            except Exception as e:  # one bad PDF shouldn't stop the batch
                log.error("%s failed: %r", pdf_path.name, e)
    finally:
        await ocr.aclose()


if __name__ == "__main__":
    asyncio.run(run())
