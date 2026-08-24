"""Structural analysis of PDFs and images.

Canonical implementation of the measurements the Phase 0 corpus survey pioneered:
text-layer detection, embedded image resolution, relative sharpness, rotation and script
mix. `tools/corpus_survey.py` uses this module so that the survey and the production
quality gate cannot drift apart — a threshold tuned against survey numbers has to mean the
same thing at ingest time.

PRIVACY: this module computes *metrics* about text. It counts characters and classifies
scripts. It never returns, logs or prints document text. The one place text is exposed is
`page_text()`, which exists for the extraction pipeline and is not used by the quality gate.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

try:
    import pypdfium2 as pdfium

    HAVE_PDFIUM = True
except Exception:  # pragma: no cover
    HAVE_PDFIUM = False

try:
    import numpy as np

    HAVE_NUMPY = True
except Exception:  # pragma: no cover
    HAVE_NUMPY = False

try:
    from PIL import Image

    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False


# Text-layer classification, in characters per page (median across the document).
DIGITAL_MIN_CHARS_PER_PAGE: Final = 200
SCANNED_MAX_CHARS_PER_PAGE: Final = 20

RENDER_SAMPLE_PAGES: Final = 5
RENDER_SCALE: Final = 2.0  # ~144 dpi equivalent

DEVANAGARI: Final = (0x0900, 0x097F)
OTHER_INDIC: Final = (0x0980, 0x0DFF)


class TextLayer:
    DIGITAL = "DIGITAL"
    MIXED = "MIXED"
    SCANNED = "SCANNED"
    UNKNOWN = "UNKNOWN"


@dataclass
class PageInfo:
    number: int
    width_pt: float | None = None
    height_pt: float | None = None
    rotation: int = 0
    text_chars: int = 0
    embedded_dpi: list[float] = field(default_factory=list)
    sharpness: float | None = None


@dataclass
class DocumentInfo:
    """Everything the quality gate needs, and nothing that reveals content."""

    ok: bool = True
    error: str | None = None
    encrypted: bool = False

    page_count: int = 0
    pages: list[PageInfo] = field(default_factory=list)

    total_text_chars: int = 0
    median_chars_per_page: float | None = None
    text_layer: str = TextLayer.UNKNOWN

    min_embedded_dpi: float | None = None
    median_sharpness: float | None = None

    devanagari_chars: int = 0
    other_indic_chars: int = 0
    non_latin_ratio: float | None = None

    producer: str | None = None

    @property
    def rotated_pages(self) -> int:
        return sum(1 for p in self.pages if p.rotation % 360 != 0)

    @property
    def distinct_page_sizes(self) -> int:
        sizes = {
            (round(p.width_pt or 0), round(p.height_pt or 0))
            for p in self.pages
            if p.width_pt
        }
        return len(sizes)

    @property
    def needs_ocr(self) -> bool:
        """True unless a trustworthy embedded text layer covers the document.

        MIXED needs per-PAGE routing, not per-document, so it counts as needing OCR at the
        document level. Never OCR what you can read, but never trust a partial text layer.
        """
        return self.text_layer != TextLayer.DIGITAL


def count_scripts(text: str) -> tuple[int, int, int]:
    """(devanagari, other_indic, latin) character counts."""
    dev = other = latin = 0
    for ch in text:
        cp = ord(ch)
        if DEVANAGARI[0] <= cp <= DEVANAGARI[1]:
            dev += 1
        elif OTHER_INDIC[0] <= cp <= OTHER_INDIC[1]:
            other += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            latin += 1
    return dev, other, latin


def _laplacian_variance(gray) -> float:
    """Variance of the Laplacian - a relative sharpness proxy.

    Comparable only between pages rendered at the same scale, which is why the quality
    gate treats its threshold as a tunable corpus-relative figure rather than a constant.
    """
    g = gray.astype("float32")
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def _to_gray(arr):
    if arr.ndim == 2:
        return arr
    a = arr[:, :, :3].astype("float32")
    return 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]


def _sample_indices(n: int, k: int) -> list[int]:
    if n <= k:
        return list(range(n))
    step = (n - 1) / (k - 1)
    return sorted({int(round(i * step)) for i in range(k)})


def _classify_text_layer(per_page_chars: list[int]) -> tuple[str, float | None]:
    if not per_page_chars:
        return TextLayer.UNKNOWN, None
    median = float(statistics.median(per_page_chars))
    if median >= DIGITAL_MIN_CHARS_PER_PAGE:
        return TextLayer.DIGITAL, median
    if median <= SCANNED_MAX_CHARS_PER_PAGE:
        return TextLayer.SCANNED, median
    return TextLayer.MIXED, median


def analyse_pdf(source: str | Path | bytes, *, measure_sharpness: bool = True) -> DocumentInfo:
    """Analyse a PDF from a path or raw bytes."""
    info = DocumentInfo()
    if not HAVE_PDFIUM:
        info.ok, info.error = False, "pypdfium2 not installed"
        return info

    try:
        pdf = pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            info.encrypted = True
            info.ok, info.error = False, "encrypted"
        else:
            info.ok, info.error = False, f"open_failed: {type(exc).__name__}"
        return info

    try:
        info.page_count = len(pdf)
        try:
            meta = pdf.get_metadata_dict()
            info.producer = meta.get("Producer") or meta.get("Creator") or None
        except Exception:
            pass

        per_page_chars: list[int] = []
        dev = other = latin = 0
        all_dpi: list[float] = []

        for i in range(info.page_count):
            pi = PageInfo(number=i + 1)
            try:
                page = pdf[i]
            except Exception:
                per_page_chars.append(0)
                info.pages.append(pi)
                continue

            try:
                pi.rotation = int(page.get_rotation())
            except Exception:
                pass
            try:
                w, h = page.get_size()
                pi.width_pt, pi.height_pt = float(w), float(h)
            except Exception:
                pass

            text = ""
            try:
                text = page.get_textpage().get_text_bounded()
            except Exception:
                text = ""

            pi.text_chars = len(text)
            per_page_chars.append(pi.text_chars)
            d, o, la = count_scripts(text)
            dev, other, latin = dev + d, other + o, latin + la

            try:
                for obj in page.get_objects():
                    md = getattr(obj, "get_metadata", None)
                    if md is None:
                        continue
                    try:
                        m = md()
                    except Exception:
                        continue
                    for attr in ("horizontal_dpi", "vertical_dpi"):
                        val = getattr(m, attr, None)
                        if val and 10 < float(val) < 2400:
                            pi.embedded_dpi.append(round(float(val), 1))
            except Exception:
                pass
            all_dpi.extend(pi.embedded_dpi)
            info.pages.append(pi)

        info.total_text_chars = sum(per_page_chars)
        info.devanagari_chars, info.other_indic_chars = dev, other
        denom = dev + other + latin
        info.non_latin_ratio = round((dev + other) / denom, 4) if denom else None
        info.text_layer, info.median_chars_per_page = _classify_text_layer(per_page_chars)
        info.min_embedded_dpi = min(all_dpi) if all_dpi else None

        if measure_sharpness and HAVE_NUMPY and info.needs_ocr and info.page_count:
            samples: list[float] = []
            for idx in _sample_indices(info.page_count, RENDER_SAMPLE_PAGES):
                try:
                    arr = pdf[idx].render(scale=RENDER_SCALE).to_numpy()
                    s = round(_laplacian_variance(_to_gray(arr)), 2)
                    samples.append(s)
                    info.pages[idx].sharpness = s
                except Exception:
                    continue
            if samples:
                info.median_sharpness = float(statistics.median(samples))
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    return info


def analyse_image(source: str | Path) -> DocumentInfo:
    """Analyse a standalone image, as a phone photo of a document would arrive."""
    info = DocumentInfo()
    if not HAVE_PIL:
        info.ok, info.error = False, "Pillow not installed"
        return info
    try:
        with Image.open(source) as im:
            info.page_count = getattr(im, "n_frames", 1)
            pi = PageInfo(number=1, width_pt=float(im.width), height_pt=float(im.height))
            dpi = im.info.get("dpi")
            if dpi and dpi[0]:
                pi.embedded_dpi.append(round(float(dpi[0]), 1))
                info.min_embedded_dpi = pi.embedded_dpi[0]
            # An image has no text layer by construction.
            info.text_layer = TextLayer.SCANNED
            info.median_chars_per_page = 0.0
            if HAVE_NUMPY:
                arr = np.asarray(im.convert("RGB"))
                pi.sharpness = round(_laplacian_variance(_to_gray(arr)), 2)
                info.median_sharpness = pi.sharpness
            info.pages.append(pi)
    except Exception as exc:
        info.ok, info.error = False, f"open_failed: {type(exc).__name__}"
    return info


def analyse(source: str | Path) -> DocumentInfo:
    """Dispatch on file extension."""
    p = Path(source)
    if p.suffix.lower() == ".pdf":
        return analyse_pdf(p)
    return analyse_image(p)


def page_text(source: str | Path | bytes, page: int) -> str:
    """Extracted text for one page, 1-indexed.

    For the extraction pipeline. NOT used by the quality gate, and callers must respect
    docs/privacy/data-handling-policy.md - this returns customer document content.
    """
    if not HAVE_PDFIUM:
        raise RuntimeError("pypdfium2 not installed")
    pdf = pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
    try:
        return pdf[page - 1].get_textpage().get_text_bounded()
    finally:
        pdf.close()
