"""Provenance: where a claim came from.

Every assertion in this system must answer "where did this come from?" with enough
precision that a Risk Manager can click it and land on the exact region of the exact page.
That requirement is what makes bounding boxes the real product of OCR, not text.

There are four kinds of origin, and they are deliberately not interchangeable:

* `DocumentProvenance`  - extracted from an uploaded customer document
* `ExternalProvenance`  - observed from an authoritative external source
* `HumanProvenance`     - asserted or corrected by a reviewer
* `DerivedProvenance`   - computed from other claims

A derived value that cannot name its inputs is not traceable, so `DerivedProvenance`
requires them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class BoundingBox(BaseModel):
    """Region on a page, in PDF points, origin top-left.

    Stored per claim so the review UI can highlight the exact source region. Page numbers
    are 1-indexed to match what the reviewer sees in the viewer.
    """

    model_config = ConfigDict(frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    def model_post_init(self, __context) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError(f"degenerate bounding box: {self}")

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


class TextSpan(BaseModel):
    """Character offsets into a page's extracted text.

    Complements the bounding box. The span is what the span-grounding verifier checks
    against (ADR-0004): a value the model cannot locate in the extracted text is
    discarded rather than reported.
    """

    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    def model_post_init(self, __context) -> None:
        if self.end < self.start:
            raise ValueError(f"span end before start: {self}")


class DocumentProvenance(BaseModel):
    """Origin: an uploaded customer document."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["document"] = "document"
    document_id: str
    page: int = Field(ge=1)
    bbox: BoundingBox | None = None
    span: TextSpan | None = None
    #: The exact text the value was read from. Short by design - this is an evidence
    #: snippet for the reviewer, not a copy of the document.
    source_text: str | None = Field(default=None, max_length=2000)
    #: Per-token OCR confidence for the source region, where OCR was used at all.
    #: None means the value came from an embedded text layer, which is more reliable.
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def from_text_layer(self) -> bool:
        return self.ocr_confidence is None


class ExternalProvenance(BaseModel):
    """Origin: an authoritative external source.

    `retrieved_at` is mandatory and `snapshot_id` points at the stored artefact. External
    data is snapshotted, never re-fetched during a re-run - otherwise a case cannot be
    reproduced, because the outside world will have moved.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["external"] = "external"
    #: `verification_sources[].id` from docs/regulatory/sources.yaml, e.g. SRC_CERSAI.
    source_id: str
    authority: str
    retrieved_at: datetime
    #: Immutable stored artefact: response payload or rendered page capture.
    snapshot_id: str
    #: Access tier at time of retrieval (T1..T6). Caps the confidence of the observation.
    tier: str | None = None
    #: Set when a human operated the source on our behalf (T4/T5).
    operator_id: str | None = None
    url: str | None = None


class HumanProvenance(BaseModel):
    """Origin: a reviewer asserted, corrected or overrode a value.

    Human input is the highest-authority origin in the system and is also the training
    signal for calibration. `supersedes` records what was overridden so the original
    machine claim is never lost - an override must remain auditable.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["human"] = "human"
    actor: str
    asserted_at: datetime
    rationale: str | None = None
    supersedes: str | None = None


class DerivedProvenance(BaseModel):
    """Origin: computed from other claims."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["derived"] = "derived"
    #: Claim ids this value was computed from. Non-empty by construction: a derived value
    #: that cannot name its inputs is not traceable.
    input_claim_ids: list[str] = Field(min_length=1)
    #: Identifier of the computation, so the derivation can be replayed.
    method: str
    method_version: str


Provenance = Annotated[
    DocumentProvenance | ExternalProvenance | HumanProvenance | DerivedProvenance,
    Field(discriminator="kind"),
]


class ProcessingContext(BaseModel):
    """The versions in effect when a case was processed.

    Pinned per run so findings are reproducible. If any of these changes, the same case
    may legitimately produce different findings - and the reviewer is entitled to know
    which version produced the result they are looking at.
    """

    model_config = ConfigDict(frozen=True)

    pipeline_version: str
    rule_set_version: str
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    #: Effective date used to select applicable regulatory requirements. Regulation is
    #: versioned, so a case must be evaluated against the rules in force at a stated date.
    regulatory_as_of: date
    processed_at: datetime
