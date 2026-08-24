"""Text extraction results.

COORDINATE SYSTEM
-----------------
Everything here is **top-left origin, PDF points**. This is not the native system of
either source, so both are converted on the way in:

* PDFium text rectangles are `(left, bottom, right, top)` in points with a **bottom-left**
  origin, so y is flipped against page height.
* OCR returns **top-left pixel** coordinates at whatever scale the page was rendered, so
  x and y are divided by that scale.

Getting this wrong does not crash anything — it silently highlights the wrong region of
the page when a reviewer clicks a finding, which destroys trust in every citation the
system produces. Hence one normalised system, converted at the edges, and tested.

CHARACTER OFFSETS
-----------------
Each block records its span in the assembled page text. That is what lets a
`DocumentProvenance` carry both a `TextSpan` and a `BoundingBox` for the same value, and
what the span-grounding verifier (ADR-0004) checks against.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..model.provenance import BoundingBox, TextSpan


class TextSource(StrEnum):
    """Where a page's text came from. Affects how much it can be trusted."""

    #: Embedded PDF text layer. Exact, no recognition error.
    TEXT_LAYER = "text_layer"
    #: Optical recognition. Carries per-block confidence.
    OCR = "ocr"
    #: Nothing found by either route.
    EMPTY = "empty"


class TextBlock(BaseModel):
    """One recognised or extracted run of text, with its position."""

    model_config = ConfigDict(frozen=True)

    text: str
    bbox: BoundingBox
    #: None means the text came from an embedded text layer, where there is no
    #: recognition uncertainty. That is materially different from a confidence of 1.0.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: TextSource = TextSource.OCR
    #: Span in the assembled page text.
    char_start: int = 0
    char_end: int = 0

    @property
    def span(self) -> TextSpan:
        return TextSpan(start=self.char_start, end=self.char_end)

    @property
    def is_exact(self) -> bool:
        return self.source is TextSource.TEXT_LAYER


class OcrPage(BaseModel):
    """Extraction result for one page."""

    model_config = ConfigDict(frozen=True)

    page: int = Field(ge=1)
    width_pt: float
    height_pt: float
    source: TextSource
    blocks: list[TextBlock] = Field(default_factory=list)
    text: str = ""
    #: Engine identifier, for reproducibility. None for text-layer pages.
    engine: str | None = None

    @property
    def mean_confidence(self) -> float | None:
        """Mean OCR confidence, or None for exact text.

        Deliberately not defaulted to 1.0 for text-layer pages: "no recognition step
        happened" and "recognition was perfectly confident" are different claims.
        """
        vals = [b.confidence for b in self.blocks if b.confidence is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    @property
    def min_confidence(self) -> float | None:
        vals = [b.confidence for b in self.blocks if b.confidence is not None]
        return round(min(vals), 4) if vals else None

    @property
    def char_count(self) -> int:
        return len(self.text)

    def block_at(self, offset: int) -> TextBlock | None:
        """The block containing a character offset. Maps a span back to a bbox."""
        for b in self.blocks:
            if b.char_start <= offset < b.char_end:
                return b
        return None

    def blocks_for_span(self, start: int, end: int) -> list[TextBlock]:
        """Every block overlapping a span, for highlighting a multi-line value."""
        return [b for b in self.blocks if b.char_start < end and b.char_end > start]


class OcrDocument(BaseModel):
    """Extraction result for a whole document."""

    model_config = ConfigDict(frozen=True)

    document_sha256: str
    pages: list[OcrPage] = Field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, number: int) -> OcrPage | None:
        return next((p for p in self.pages if p.page == number), None)

    def page_texts(self) -> list[str]:
        """Text per page, ordered - the input the classifier expects."""
        return [p.text for p in sorted(self.pages, key=lambda p: p.page)]

    @property
    def sources(self) -> dict[str, int]:
        """Page counts by source. A MIXED document shows both."""
        out: dict[str, int] = {}
        for p in self.pages:
            out[p.source.value] = out.get(p.source.value, 0) + 1
        return out

    @property
    def mean_confidence(self) -> float | None:
        vals = [c for p in self.pages if (c := p.mean_confidence) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for p in self.pages if p.source is TextSource.OCR)


def assemble_page(
    page: int,
    width_pt: float,
    height_pt: float,
    raw_blocks: Iterable[tuple[str, BoundingBox, float | None]],
    source: TextSource,
    engine: str | None = None,
    *,
    separator: str = "\n",
) -> OcrPage:
    """Join blocks into page text, assigning each its character span.

    Blocks are ordered top-to-bottom then left-to-right before assembly. That is a crude
    reading order and is wrong for multi-column layouts; proper reading-order analysis
    belongs with layout detection, not here. Recorded rather than pretended away.
    """
    ordered = sorted(
        [(t, b, c) for t, b, c in raw_blocks if t],
        key=lambda item: (round(item[1].y0, 1), round(item[1].x0, 1)),
    )

    blocks: list[TextBlock] = []
    parts: list[str] = []
    cursor = 0
    for text, bbox, conf in ordered:
        start = cursor
        end = start + len(text)
        blocks.append(TextBlock(
            text=text, bbox=bbox, confidence=conf, source=source,
            char_start=start, char_end=end,
        ))
        parts.append(text)
        cursor = end + len(separator)

    return OcrPage(
        page=page,
        width_pt=width_pt,
        height_pt=height_pt,
        source=source if blocks else TextSource.EMPTY,
        blocks=blocks,
        text=separator.join(parts),
        engine=engine if blocks else None,
    )
