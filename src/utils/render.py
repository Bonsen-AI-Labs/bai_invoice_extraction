"""PDF render + crop for LLM arbitration.

DI reports PDF polygons in inches (origin top-left). A crop is the axis-aligned
bounding box of the polygon, in pixels at RENDER_DPI, plus padding. No rotation
is applied to the raster — coordinate-space geometry stays in sync.
Renders are in-memory only in Phase 1 (no Blob).
"""

from __future__ import annotations

import fitz  # pymupdf

from src.config import PAD_FRAC, PAD_MIN_PX, RENDER_DPI

_PT_PER_INCH = 72.0


def poly_bbox(polygon: list[float]) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) over a flattened 8-float polygon (inches)."""
    xs = polygon[0::2]
    ys = polygon[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def polygon_to_pixels(
    polygon: list[float], dpi: int = RENDER_DPI, pad_px: int = PAD_MIN_PX
) -> tuple[int, int, int, int]:
    """Pure math: polygon (inches) -> padded pixel box. Testable without fitz."""
    min_x, min_y, max_x, max_y = poly_bbox(polygon)
    pad_x = max(PAD_FRAC * (max_x - min_x) * dpi, pad_px)
    pad_y = max(PAD_FRAC * (max_y - min_y) * dpi, pad_px)
    return (
        int(min_x * dpi - pad_x),
        int(min_y * dpi - pad_y),
        int(max_x * dpi + pad_x),
        int(max_y * dpi + pad_y),
    )


def crop_region(
    pdf_bytes: bytes,
    page_index: int,
    polygon: list[float],
    dpi: int = RENDER_DPI,
    pad_px: int = PAD_MIN_PX,
) -> bytes:
    """Render the padded polygon region of one page to PNG bytes."""
    min_x, min_y, max_x, max_y = poly_bbox(polygon)
    pad_in = pad_px / dpi
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page = doc[page_index]
        r = page.rect
        clip = fitz.Rect(
            max(r.x0, (min_x - pad_in) * _PT_PER_INCH),
            max(r.y0, (min_y - pad_in) * _PT_PER_INCH),
            min(r.x1, (max_x + pad_in) * _PT_PER_INCH),
            min(r.y1, (max_y + pad_in) * _PT_PER_INCH),
        )
        zoom = dpi / _PT_PER_INCH
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        return pix.tobytes("png")


def _demo() -> None:
    # Crop math: a 1"x0.2" box at (4.9,0.8) inches, 300 dpi, 24px pad.
    box = polygon_to_pixels([4.9, 0.8, 5.9, 0.8, 5.9, 1.0, 4.9, 1.0], 300, 24)
    assert box == (1434, 216, 1806, 324), box
    print("render self-check ok")


if __name__ == "__main__":
    _demo()
