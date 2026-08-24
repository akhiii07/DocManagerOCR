"""Text extraction service: per-page routing between text layer and OCR.

The routing decision is made **per page, not per document**. A bundle where a scanned
annexure has been appended to a digitally generated deed is ordinary, and treating the
whole file one way either wastes accuracy on the digital pages or wastes GPU time on them.
This closes the `MIXED` text-layer case that the quality gate flags.

    for each page:
        text layer has usable text?  -> extract it exactly (no recognition error)
        otherwise, engine available? -> render at target dpi, recognise
        otherwise                    -> EMPTY, with the reason recorded

An unavailable engine degrades to text-layer-only extraction rather than failing. A
deployment with no OCR installed still processes digital PDFs, and the pages it cannot
read are reported as empty rather than silently dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .cache import NullOcrCache, OcrCache, cache_key
from .engine import OcrEngine, RenderedPage, UnavailableEngine
from .textlayer import extract_page as extract_text_layer
from .textlayer import page_has_usable_text
from .types import OcrDocument, OcrPage, TextSource, assemble_page

log = logging.getLogger(__name__)

try:
    import pypdfium2 as pdfium

    HAVE_PDFIUM = True
except Exception:  # pragma: no cover
    HAVE_PDFIUM = False


#: A page whose text layer yields fewer characters than this is treated as scanned.
#: Matches the corpus-survey threshold in dmocr.ingest.pdfinfo so the survey's per-page
#: picture and the extraction router agree.
DEFAULT_MIN_TEXT_CHARS = 20


@dataclass
class ExtractionStats:
    text_layer_pages: int = 0
    ocr_pages: int = 0
    empty_pages: int = 0
    cache_hits: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.text_layer_pages + self.ocr_pages + self.empty_pages


class TextExtractionService:
    def __init__(
        self,
        engine: OcrEngine | None = None,
        cache: OcrCache | None = None,
        *,
        min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    ):
        # `is None`, NOT `or`. Caches define __len__, so an EMPTY cache is falsy and
        # `cache or NullOcrCache()` would silently discard a freshly constructed one -
        # caching would then appear to work while never storing anything.
        self.engine = engine if engine is not None else UnavailableEngine(
            "no engine configured"
        )
        self.cache = cache if cache is not None else NullOcrCache()
        self.min_text_chars = min_text_chars

    # -- public API --------------------------------------------------------------

    def extract(
        self,
        data: bytes,
        sha256: str,
        *,
        pages: list[int] | None = None,
    ) -> tuple[OcrDocument, ExtractionStats]:
        """Extract text from a PDF, routing each page independently."""
        stats = ExtractionStats()
        if not HAVE_PDFIUM:
            stats.failures.append("pypdfium2 not installed")
            return OcrDocument(document_sha256=sha256), stats

        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as exc:
            stats.failures.append(f"open_failed: {type(exc).__name__}: {exc}")
            return OcrDocument(document_sha256=sha256), stats

        try:
            wanted = pages or list(range(1, len(pdf) + 1))
            out: list[OcrPage] = []
            for n in wanted:
                if n < 1 or n > len(pdf):
                    stats.failures.append(f"page {n} out of range")
                    continue
                out.append(self._extract_one(pdf, sha256, n, stats))
            return OcrDocument(document_sha256=sha256, pages=out), stats
        finally:
            try:
                pdf.close()
            except Exception:
                pass

    def extract_image(self, image, sha256: str, *, width_pt: float, height_pt: float,
                      dpi: float) -> tuple[OcrDocument, ExtractionStats]:
        """Extract from a standalone image array. Always the OCR path."""
        stats = ExtractionStats()
        if not self.engine.available:
            stats.empty_pages = 1
            stats.failures.append("no OCR engine available")
            return OcrDocument(
                document_sha256=sha256,
                pages=[OcrPage(page=1, width_pt=width_pt, height_pt=height_pt,
                               source=TextSource.EMPTY)],
            ), stats

        scale = dpi / 72.0
        page = self._recognise(
            RenderedPage(image=image, scale=scale, width_pt=width_pt,
                         height_pt=height_pt),
            page_number=1,
        )
        stats.ocr_pages = 1
        return OcrDocument(document_sha256=sha256, pages=[page]), stats

    # -- internals ---------------------------------------------------------------

    def _extract_one(self, pdf, sha256: str, n: int, stats: ExtractionStats) -> OcrPage:
        # 1. Text layer, if this page has one worth using.
        if page_has_usable_text(pdf, n, self.min_text_chars):
            stats.text_layer_pages += 1
            return extract_text_layer(pdf, n)

        # 2. OCR, if an engine is available.
        if not self.engine.available:
            stats.empty_pages += 1
            page = pdf[n - 1]
            w, h = (float(v) for v in page.get_size())
            return OcrPage(page=n, width_pt=w, height_pt=h, source=TextSource.EMPTY)

        dpi = self.engine.target_dpi
        key = cache_key(sha256, n, self.engine.engine_id, dpi)
        cached = self.cache.get(key)
        if cached is not None:
            stats.cache_hits += 1
            stats.ocr_pages += 1
            return cached

        try:
            rendered = self._render(pdf, n, dpi)
        except Exception as exc:
            stats.failures.append(f"page {n} render failed: {type(exc).__name__}")
            stats.empty_pages += 1
            return OcrPage(page=n, width_pt=0.0, height_pt=0.0, source=TextSource.EMPTY)

        try:
            page = self._recognise(rendered, n)
        except Exception as exc:
            # A failed page must be visible, not silently absent from the document.
            log.exception("OCR failed on page %s", n)
            stats.failures.append(f"page {n} OCR failed: {type(exc).__name__}: {exc}")
            stats.empty_pages += 1
            return OcrPage(page=n, width_pt=rendered.width_pt,
                           height_pt=rendered.height_pt, source=TextSource.EMPTY)

        stats.ocr_pages += 1
        self.cache.put(key, page)
        return page

    @staticmethod
    def _render(pdf, n: int, dpi: float) -> RenderedPage:
        page = pdf[n - 1]
        w, h = (float(v) for v in page.get_size())
        scale = dpi / 72.0
        array = page.render(scale=scale).to_numpy()
        return RenderedPage(image=array, scale=scale, width_pt=w, height_pt=h)

    def _recognise(self, rendered: RenderedPage, page_number: int) -> OcrPage:
        blocks = self.engine.recognise(rendered)
        return assemble_page(
            page=page_number,
            width_pt=rendered.width_pt,
            height_pt=rendered.height_pt,
            raw_blocks=blocks,
            source=TextSource.OCR,
            engine=self.engine.engine_id,
        )


def default_engine(*, target_dpi: float = 200.0) -> OcrEngine:
    """The configured engine, or an UnavailableEngine explaining why there is none.

    Never raises. A machine without OCR installed should still start and process digital
    PDFs; the absence is reported where it can be handled rather than at import time.
    """
    try:
        from .rapid import RapidOcrEngine
    except Exception as exc:  # pragma: no cover
        return UnavailableEngine(f"RapidOCR adapter import failed: {exc}")

    engine = RapidOcrEngine(target_dpi=target_dpi)
    if not engine.available:
        return UnavailableEngine("rapidocr-onnxruntime not installed or failed to load")
    return engine
