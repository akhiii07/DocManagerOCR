"""Span grounding: proving a value came from the page.

ADR-0004. **A value that cannot be located in the extracted text of the document is not
extracted — it is `NOT_DETERMINABLE`.** This is the primary anti-hallucination control,
and it is the reason extraction can be trusted enough to feed compliance checks.

The control applies to every extractor, not just model-based ones. A deterministic regex
already knows where it matched, so grounding is nearly free there. The value is in making
it *impossible* to emit an ungrounded claim: `ExtractionService` will not construct a
`Claim` without a `DocumentProvenance`, and provenance can only be built by locating the
value here.

Matching is whitespace-tolerant, because OCR routinely differs from the source in spacing
("Rs. 1,25,00,000" vs "Rs.1,25,00,000"). It is NOT tolerant of differing characters — a
value whose digits do not appear on the page is rejected, which is exactly the case this
control exists for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model.provenance import DocumentProvenance
from ..ocr.types import OcrDocument, OcrPage

#: Longest source snippet kept as evidence. Enough for a reviewer to recognise the
#: context, short enough that findings are not a copy of the document.
MAX_SNIPPET = 240


@dataclass(frozen=True)
class Location:
    page: int
    start: int
    end: int
    matched_text: str


def _normalise(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace, keeping a map from normalised index back to original index."""
    out: list[str] = []
    index_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space or not out:
                continue
            out.append(" ")
            index_map.append(i)
            prev_space = True
        else:
            out.append(ch)
            index_map.append(i)
            prev_space = False
    return "".join(out), index_map


def locate_in_page(page: OcrPage, value: str) -> Location | None:
    """Find `value` in a page's text, tolerating whitespace differences."""
    if not value or not value.strip():
        return None

    # Fast path: exact substring.
    idx = page.text.find(value)
    if idx >= 0:
        return Location(page.page, idx, idx + len(value), value)

    norm_page, index_map = _normalise(page.text)
    norm_value = re.sub(r"\s+", " ", value).strip()
    if not norm_value:
        return None

    pos = norm_page.find(norm_value)
    if pos < 0:
        # Last resort: ignore whitespace entirely on both sides. Catches OCR splitting
        # or joining words inside an otherwise identical value.
        squashed_page = re.sub(r"\s+", "", page.text)
        squashed_value = re.sub(r"\s+", "", value)
        if not squashed_value or squashed_value not in squashed_page:
            return None
        # Map back approximately by scanning for the first non-space run.
        pos_sq = squashed_page.find(squashed_value)
        start = _map_squashed_index(page.text, pos_sq)
        end = _map_squashed_index(page.text, pos_sq + len(squashed_value))
        if start is None or end is None:
            return None
        return Location(page.page, start, end, page.text[start:end])

    start = index_map[pos]
    end_idx = pos + len(norm_value) - 1
    end = index_map[end_idx] + 1 if end_idx < len(index_map) else len(page.text)
    return Location(page.page, start, end, page.text[start:end])


def _map_squashed_index(text: str, squashed_index: int) -> int | None:
    """Map an index in whitespace-stripped text back to the original."""
    seen = 0
    for i, ch in enumerate(text):
        if seen == squashed_index:
            return i
        if not ch.isspace():
            seen += 1
    return len(text) if seen == squashed_index else None


def locate(document: OcrDocument, value: str, *, prefer_page: int | None = None) -> Location | None:
    """Find `value` anywhere in the document, optionally preferring one page."""
    pages = sorted(document.pages, key=lambda p: (p.page != prefer_page, p.page))
    for page in pages:
        found = locate_in_page(page, value)
        if found is not None:
            return found
    return None


def build_provenance(
    document_id: str,
    page: OcrPage,
    location: Location,
) -> DocumentProvenance:
    """Turn a location into evidence a reviewer can click.

    Carries the page, the character span, the bounding box of the block containing the
    value, a short snippet, and the OCR confidence of that block. `ocr_confidence` is None
    for text-layer pages, where no recognition step occurred.
    """
    block = page.block_at(location.start)
    snippet_start = max(0, location.start - 60)
    snippet_end = min(len(page.text), location.end + 60)
    snippet = page.text[snippet_start:snippet_end][:MAX_SNIPPET]

    from ..model.provenance import TextSpan

    return DocumentProvenance(
        document_id=document_id,
        page=page.page,
        bbox=block.bbox if block else None,
        span=TextSpan(start=location.start, end=location.end),
        source_text=snippet,
        ocr_confidence=block.confidence if block else None,
    )


class GroundingError(Exception):
    """Raised when a value cannot be located in the document."""


def ground(
    document: OcrDocument,
    document_id: str,
    value: str,
    *,
    prefer_page: int | None = None,
) -> DocumentProvenance:
    """Locate `value` and build provenance, or raise.

    Raising rather than returning None is deliberate: a caller that wants to emit a claim
    must handle the failure explicitly. Silently returning None invites `provenance or
    some_default`, which is how ungrounded values get emitted.
    """
    location = locate(document, value, prefer_page=prefer_page)
    if location is None:
        raise GroundingError(
            f"value not found in document {document_id}; refusing to emit an "
            f"ungrounded claim"
        )
    page = document.page(location.page)
    if page is None:  # pragma: no cover - locate only returns real pages
        raise GroundingError(f"page {location.page} missing from document {document_id}")
    return build_provenance(document_id, page, location)


def is_grounded(document: OcrDocument, value: str) -> bool:
    return locate(document, value) is not None
