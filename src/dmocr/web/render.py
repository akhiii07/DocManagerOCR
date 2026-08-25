"""Evidence rendering: crop the exact region a value was read from.

The brief calls "click a finding, land on the evidence" the most important thing the UI
does, and the model already carries page + bounding box for every claim.

Deliberately NOT PDF.js. Two reasons:

* We already render pages with pypdfium2 and hold the box in PDF points, so a server-side
  crop is a few lines and needs no JavaScript PDF library.
* A no-egress deployment cannot pull a viewer from a CDN, so the alternative would be
  vendoring and maintaining one.

The crop includes context around the box, because a value shown in isolation is hard to
place - a reviewer needs the surrounding line to recognise what they are looking at.
"""

from __future__ import annotations

import io
import logging

from ..model.provenance import BoundingBox

log = logging.getLogger(__name__)

#: Render resolution for crops. Higher than screen so small print stays legible.
CROP_DPI = 160.0
#: Context in PDF points kept around the box.
PAD_X = 24.0
PAD_Y = 14.0
#: Whole-page fallback resolution.
PAGE_DPI = 110.0


def _render_page(data: bytes, page_number: int, dpi: float):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        if page_number < 1 or page_number > len(pdf):
            return None, None
        page = pdf[page_number - 1]
        width_pt, height_pt = (float(v) for v in page.get_size())
        scale = dpi / 72.0
        image = page.render(scale=scale).to_pil()
        return image, (width_pt, height_pt, scale)
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def crop_evidence(
    data: bytes, page_number: int, bbox: BoundingBox | None
) -> bytes | None:
    """PNG bytes of the region a value came from, with context. None if unrenderable.

    Falls back to the whole page when there is no box, which happens for values read from
    a page with no usable block geometry. Showing the page is still better than showing
    nothing - the reviewer can find it themselves.
    """
    try:
        image, geometry = _render_page(data, page_number, CROP_DPI if bbox else PAGE_DPI)
    except Exception as exc:
        log.warning("could not render page %s: %s", page_number, exc)
        return None
    if image is None or geometry is None:
        return None

    if bbox is not None:
        _, height_pt, scale = geometry
        left = max(0, int((bbox.x0 - PAD_X) * scale))
        top = max(0, int((bbox.y0 - PAD_Y) * scale))
        right = min(image.width, int((bbox.x1 + PAD_X) * scale))
        bottom = min(image.height, int((bbox.y1 + PAD_Y) * scale))
        if right > left and bottom > top:
            image = image.crop((left, top, right, bottom))
            image = _draw_marker(image, bbox, scale, left, top)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_marker(image, bbox: BoundingBox, scale: float, left: int, top: int):
    """Outline the exact box inside the padded crop.

    Without it the reviewer sees a band of text and has to guess which part the system
    actually read - which is the question the crop exists to answer.
    """
    try:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [
                int(bbox.x0 * scale) - left,
                int(bbox.y0 * scale) - top,
                int(bbox.x1 * scale) - left,
                int(bbox.y1 * scale) - top,
            ],
            outline=(200, 30, 30),
            width=2,
        )
    except Exception:  # pragma: no cover - marker is cosmetic
        pass
    return image
