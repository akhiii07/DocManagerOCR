"""Embedded text-layer extraction.

The cheapest and most accurate path: if a PDF already carries text, read it. No
recognition step means no recognition error, so these blocks carry `confidence=None`
rather than a fabricated 1.0.

PDFium reports text rectangles as `(left, bottom, right, top)` in PDF points with a
**bottom-left** origin. The y axis is flipped here to the top-left convention used
throughout the evidence model.
"""

from __future__ import annotations

import logging

from ..model.provenance import BoundingBox
from .types import OcrPage, TextSource, assemble_page

log = logging.getLogger(__name__)

try:
    import pypdfium2 as pdfium

    HAVE_PDFIUM = True
except Exception:  # pragma: no cover
    HAVE_PDFIUM = False


def extract_page(pdf, page_number: int) -> OcrPage:
    """Extract one page's text layer. `page_number` is 1-indexed."""
    page = pdf[page_number - 1]
    width_pt, height_pt = (float(v) for v in page.get_size())

    raw: list[tuple[str, BoundingBox, float | None]] = []
    try:
        tp = page.get_textpage()
        for i in range(tp.count_rects()):
            left, bottom, right, top = tp.get_rect(i)
            text = tp.get_text_bounded(left, bottom, right, top)
            if not text or not text.strip():
                continue
            raw.append((
                text,
                BoundingBox(
                    x0=float(left),
                    # Flip: PDF y grows upward, the evidence model's y grows downward.
                    y0=height_pt - float(top),
                    x1=float(right),
                    y1=height_pt - float(bottom),
                ),
                None,  # exact text: no recognition confidence exists
            ))
    except Exception as exc:
        log.warning("text layer extraction failed on page %s: %s", page_number, exc)

    return assemble_page(
        page=page_number,
        width_pt=width_pt,
        height_pt=height_pt,
        raw_blocks=raw,
        source=TextSource.TEXT_LAYER,
        engine=None,
    )


def page_has_usable_text(pdf, page_number: int, min_chars: int) -> bool:
    """Whether a page's text layer is worth using instead of OCR.

    Applied PER PAGE, not per document. A bundle where a scanned annexure has been
    appended to a digital deed is common, and routing the whole document one way wastes
    accuracy on some pages and compute on others.
    """
    try:
        text = pdf[page_number - 1].get_textpage().get_text_bounded()
    except Exception:
        return False
    return len(text.strip()) >= min_chars
