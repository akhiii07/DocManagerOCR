"""The Case aggregate: the unit of work.

A case is a borrower, a property and a bundle of documents. It is the unit of work because
the highest-value checks - cross-document consistency and external verification - are
meaningless at the level of a single file.

Tenant and case scoping are retained here even though the MVP has no authentication
(ADR-0002). Every retrieval and every query filters on them regardless, so adding auth
later is a middleware and policy change rather than a schema migration.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    DocumentType,
    InstrumentStrength,
    SecurityType,
    instrument_strength_of,
)
from .entities import Party, Project, Property
from .provenance import ProcessingContext


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LenderType(StrEnum):
    HFC = "HFC"
    NBFC = "NBFC"
    BANK = "BANK"


class Product(StrEnum):
    HOUSING_LOAN = "housing_loan"
    LOAN_AGAINST_PROPERTY = "loan_against_property"


class DocumentQuality(StrEnum):
    """Outcome of the quality gate.

    DEGRADED is the important one: it means "process, but cap confidence". Rejecting
    outright is a last resort because real-world collateral bundles are often poor
    quality, and refusing them pushes work back to a human with no explanation.
    """

    OK = "OK"
    DEGRADED = "DEGRADED"
    REJECTED = "REJECTED"


class CustodyStatus(StrEnum):
    """Whether the lender holds the original.

    Present because of two joined findings. TPA s.58(f)/s.59 make the Mumbai security an
    equitable mortgage created by DEPOSIT OF ORIGINAL TITLE DEEDS, so the originals held
    ARE the security. And INST_RBI_RELEASE_DOCS_2023 requires their release within 30 days
    of full repayment, at Rs.5,000 per day of delay. Both require the lender to know
    exactly which originals it holds, per case.
    """

    ORIGINAL_HELD = "original_held"
    CERTIFIED_COPY = "certified_copy"
    PHOTOCOPY = "photocopy"
    NOT_RECEIVED = "not_received"
    RELEASED = "released"
    UNKNOWN = "unknown"


class Document(BaseModel):
    """An uploaded document and what we know about it."""

    model_config = ConfigDict(frozen=False)

    document_id: str = Field(default_factory=lambda: _new_id("DOC"))
    case_id: str
    tenant_id: str

    #: Classifier output. UNKNOWN routes to a human rather than guessing a schema -
    #: applying the wrong extraction schema produces confidently wrong fields.
    document_type: DocumentType = DocumentType.UNKNOWN
    classification_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    #: Content hash, for deduplication and for tying evidence to exact bytes.
    sha256: str
    page_count: int | None = Field(default=None, ge=1)
    quality: DocumentQuality = DocumentQuality.OK
    quality_notes: list[str] = Field(default_factory=list)

    #: True when a usable embedded text layer was found, so OCR was skipped.
    #: Never OCR what you can read.
    has_text_layer: bool = False

    custody: CustodyStatus = CustodyStatus.UNKNOWN
    received_at: datetime | None = None

    @property
    def instrument_strength(self) -> InstrumentStrength:
        """What this document can establish about ownership."""
        return instrument_strength_of(self.document_type)

    @property
    def confidence_capped(self) -> bool:
        return self.quality is DocumentQuality.DEGRADED


class Case(BaseModel):
    """A borrower + a property + a document bundle."""

    model_config = ConfigDict(frozen=False)

    case_id: str = Field(default_factory=lambda: _new_id("CASE"))
    #: Retained despite ADR-0002. Scoping exists; it is simply not yet enforced against an
    #: authenticated principal.
    tenant_id: str

    lender_type: LenderType
    product: Product

    #: Jurisdiction drives BOTH regulatory applicability and which external verification
    #: sources are in scope. MVP is Mumbai / Maharashtra only.
    state: str = "MH"
    city: str = "Mumbai"

    #: Security type gates TPA s.59. Defaults to UNKNOWN, never to a guess - asserting a
    #: registration defect without knowing the security type would be a false positive.
    security_type: SecurityType = SecurityType.UNKNOWN

    documents: list[Document] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)
    parties: list[Party] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)

    #: Which document types this case is expected to contain. Drives MISSING rather than
    #: NOT_APPLICABLE for absent documents.
    expected_documents: list[DocumentType] = Field(default_factory=list)

    processing_context: ProcessingContext | None = None
    opened_at: datetime = Field(default_factory=lambda: datetime.now())

    # -- documents ---------------------------------------------------------------

    def add_document(self, doc: Document) -> None:
        if doc.case_id != self.case_id:
            raise ValueError(f"document {doc.document_id} belongs to case {doc.case_id}")
        if doc.tenant_id != self.tenant_id:
            raise ValueError(f"document {doc.document_id} belongs to another tenant")
        self.documents.append(doc)

    def documents_of_type(self, doc_type: DocumentType) -> list[Document]:
        return [d for d in self.documents if d.document_type == doc_type]

    def missing_document_types(self) -> list[DocumentType]:
        present = {d.document_type for d in self.documents}
        return [t for t in self.expected_documents if t not in present]

    def usable_documents(self) -> list[Document]:
        return [d for d in self.documents if d.quality is not DocumentQuality.REJECTED]

    # -- security ----------------------------------------------------------------

    def mortgage_requires_registration(self) -> bool | None:
        """TPA s.59, conditional on security type.

        Returns None when the security type is unknown, because the honest answer is
        NOT_DETERMINABLE. In Mumbai the common case is an equitable mortgage by deposit of
        title deeds, which s.59 expressly exempts - so a blanket "mortgages must be
        registered" rule would fire on a large share of sound cases.
        """
        if self.security_type is SecurityType.UNKNOWN:
            return None
        return self.security_type.requires_registered_instrument

    # -- custody -----------------------------------------------------------------

    def originals_held(self) -> list[Document]:
        return [d for d in self.documents if d.custody is CustodyStatus.ORIGINAL_HELD]

    def custody_inventory(self) -> dict[str, list[str]]:
        """Per-case inventory of what the lender physically holds.

        Required by INST_RBI_RELEASE_DOCS_2023 (30-day release, Rs.5,000/day for delay)
        and load-bearing for an equitable mortgage, where the originals are the security.
        """
        out: dict[str, list[str]] = {}
        for d in self.documents:
            out.setdefault(d.custody.value, []).append(d.document_id)
        return out
