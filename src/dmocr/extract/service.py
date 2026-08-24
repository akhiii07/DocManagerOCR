"""Extraction service: schema + finders + grounding -> claims.

The pipeline for one document:

    OcrDocument + document type
      -> select schema            (no schema -> nothing extracted, reported as such)
      -> run each field's finder over the pages in scope
      -> GROUND every candidate   (ADR-0004: unlocatable values are discarded)
      -> emit Claims with DocumentProvenance

The grounding step is not a formality. `Claim` construction here goes exclusively through
`_to_claim`, which requires a `DocumentProvenance`, and provenance can only be built by
locating the value in the page text. There is no code path that emits an ungrounded claim.

Multiple distinct values for a single-valued field are emitted as **competing claims**
rather than resolved. A deed that states two different areas is a real finding, and the
claim model already represents disagreement — so an internal contradiction surfaces
through the same machinery as a cross-document one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field

from ..model.claims import Claim, ClaimValue
from ..model.common import ConfidenceTier, DocumentType, instrument_strength_of
from ..model.provenance import DocumentProvenance
from ..ocr.types import OcrDocument, OcrPage, TextSource
from .extractors import FieldMatch
from .grounding import GroundingError, ground
from .schema import DocumentSchema, FieldSpec, PageScope, Select, schema_for

log = logging.getLogger(__name__)

#: OCR confidence at or above which an extracted value is treated as HIGH confidence.
HIGH_OCR_CONFIDENCE = 0.90
MEDIUM_OCR_CONFIDENCE = 0.70


@dataclass(frozen=True)
class ExtractedField:
    """One grounded value, ready to become a claim."""

    field_name: str
    attribute: str
    raw: str
    value: ClaimValue
    provenance: DocumentProvenance
    confidence: ConfidenceTier
    notes: list[str] = dc_field(default_factory=list)

    @property
    def page(self) -> int:
        return self.provenance.page


@dataclass
class ExtractionResult:
    document_id: str
    document_type: DocumentType
    fields: list[ExtractedField] = dc_field(default_factory=list)
    #: Required schema fields for which nothing was grounded.
    missing_required: list[str] = dc_field(default_factory=list)
    #: Candidates found but rejected because they could not be grounded. Should be rare
    #: for deterministic finders; expected to matter once a model-based extractor exists.
    rejected_ungrounded: list[str] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    @property
    def extracted_count(self) -> int:
        return len(self.fields)

    def by_attribute(self, attribute: str) -> list[ExtractedField]:
        return [f for f in self.fields if f.attribute == attribute]

    def field_names(self) -> set[str]:
        return {f.field_name for f in self.fields}


def _pages_in_scope(document: OcrDocument, scope: PageScope) -> list[OcrPage]:
    pages = sorted(document.pages, key=lambda p: p.page)
    readable = [p for p in pages if p.source is not TextSource.EMPTY and p.text]
    if not readable:
        return []
    if scope is PageScope.FIRST:
        return readable[:1]
    if scope is PageScope.LAST:
        return readable[-1:]
    return readable


def _confidence_for(provenance: DocumentProvenance, notes: list[str]) -> ConfidenceTier:
    """Derive confidence from how the text was obtained, not from the value itself."""
    # Any note flagging a contradiction caps confidence regardless of OCR quality.
    if any("does not match" in n or "possible OCR error" in n for n in notes):
        return ConfidenceTier.LOW

    if provenance.from_text_layer:
        return ConfidenceTier.HIGH  # exact text, no recognition step

    conf = provenance.ocr_confidence
    if conf is None:
        return ConfidenceTier.LOW
    if conf >= HIGH_OCR_CONFIDENCE:
        return ConfidenceTier.HIGH
    if conf >= MEDIUM_OCR_CONFIDENCE:
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


Candidate = tuple[FieldMatch, int]  # (match, page number)


def _select(candidates: list[Candidate], policy: Select) -> list[Candidate]:
    """Apply the field's multiplicity policy to candidates, preserving page numbers."""
    if not candidates:
        return []
    if policy is Select.FIRST:
        return candidates[:1]
    if policy is Select.ALL:
        return candidates

    seen: set = set()
    out: list[Candidate] = []
    for match, page in candidates:
        try:
            key = (match.value.kind, match.value.comparable())
        except Exception:
            # A value type without a usable comparable key falls back to its raw text,
            # which over-reports duplicates rather than collapsing distinct values.
            key = (match.value.kind, match.raw)
        if key in seen:
            continue
        seen.add(key)
        out.append((match, page))
    return out


class ExtractionService:
    def __init__(self, *, schemas=None):
        self._schema_lookup = schemas or schema_for

    def extract(
        self,
        document: OcrDocument,
        *,
        document_id: str,
        document_type: DocumentType,
    ) -> ExtractionResult:
        result = ExtractionResult(document_id=document_id, document_type=document_type)

        schema: DocumentSchema | None = self._schema_lookup(document_type)
        if schema is None:
            result.notes.append(
                f"No extraction schema for {document_type.value}; nothing extracted. "
                f"An unclassified or catch-all document must not acquire a schema."
            )
            return result

        if not any(p.text for p in document.pages):
            result.notes.append(
                "No text available. A scanned document must be OCR'd before extraction."
            )
            result.missing_required = [f.name for f in schema.required_fields()]
            return result

        for spec in schema.fields:
            self._extract_field(document, document_id, spec, result)

        found = result.field_names()
        result.missing_required = [
            f.name for f in schema.required_fields() if f.name not in found
        ]
        return result

    def _extract_field(
        self,
        document: OcrDocument,
        document_id: str,
        spec: FieldSpec,
        result: ExtractionResult,
    ) -> None:
        candidates: list[Candidate] = []
        for page in _pages_in_scope(document, spec.pages):
            try:
                matches = spec.finder(page.text)
            except Exception as exc:  # a broken finder must not lose the whole document
                log.exception("finder %s failed on page %s", spec.name, page.page)
                result.notes.append(
                    f"{spec.name}: finder failed on page {page.page} "
                    f"({type(exc).__name__}); field not extracted."
                )
                continue
            candidates.extend((m, page.page) for m in matches)

        for match, page_number in _select(candidates, spec.select):
            try:
                provenance = ground(document, document_id, match.raw,
                                    prefer_page=page_number)
            except GroundingError:
                # ADR-0004. A value we cannot point to on the page is not extracted.
                result.rejected_ungrounded.append(f"{spec.name}: {match.raw[:60]}")
                continue

            result.fields.append(ExtractedField(
                field_name=spec.name,
                attribute=spec.attribute,
                raw=match.raw,
                value=match.value,
                provenance=provenance,
                confidence=_confidence_for(provenance, match.notes),
                notes=list(match.notes),
            ))

    # -- claims ------------------------------------------------------------------

    @staticmethod
    def to_claims(
        result: ExtractionResult,
        *,
        subject_id: str,
        attributes: set[str] | None = None,
    ) -> list[Claim]:
        """Convert grounded fields into claims for a canonical entity.

        `instrument_strength` comes from the DOCUMENT TYPE, not the field, so an ownership
        claim carries what the instrument is legally capable of establishing. That is what
        stops an Agreement of Sale answering "who owns this?".
        """
        strength = instrument_strength_of(result.document_type)
        claims: list[Claim] = []
        for f in result.fields:
            if attributes is not None and f.attribute not in attributes:
                continue
            claims.append(Claim(
                subject_id=subject_id,
                attribute=f.attribute,
                value=f.value,
                provenance=f.provenance,
                confidence=f.confidence,
                instrument_strength=strength,
            ))
        return claims
