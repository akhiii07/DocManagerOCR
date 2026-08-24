"""Per-document-type extraction schemas.

One generic schema across all document types would be wrong. A Sale Deed has a vendor, a
purchaser and a consideration; a Property Tax bill has an assessee and an assessment
number; a Possession Letter has a handover date and little else. Applying the wrong schema
does not fail loudly — it yields a plausible-looking set of empty or mis-assigned fields,
which is why classification routes `UNKNOWN` to a human rather than guessing.

Each field names the **canonical model attribute** it feeds, so extraction output flows
into claim sets without a translation layer in between.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import StrEnum

from ..model.common import DocumentType
from . import extractors as ex
from .extractors import FieldFinder


class Select(StrEnum):
    """What to do when a finder returns several candidates."""

    #: Keep the first. For fields where repetition is restatement, not disagreement.
    FIRST = "first"
    #: Keep every candidate. Distinct values become competing claims, so an internal
    #: contradiction inside ONE document surfaces the same way a cross-document one does.
    ALL = "all"
    #: Keep every distinct value, collapsing exact repeats.
    DISTINCT = "distinct"


class PageScope(StrEnum):
    ALL = "all"
    FIRST = "first"
    #: Registration endorsements and Index II details sit at the end of a deed.
    LAST = "last"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    #: Canonical model attribute this feeds, e.g. "transaction.consideration".
    attribute: str
    finder: FieldFinder
    required: bool = False
    select: Select = Select.FIRST
    pages: PageScope = PageScope.ALL
    description: str = ""


@dataclass(frozen=True)
class DocumentSchema:
    document_type: DocumentType
    fields: list[FieldSpec] = dc_field(default_factory=list)

    def required_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if f.required]

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


# =====================================================================================
# Shared field groups
# =====================================================================================


def _property_identity_fields() -> list[FieldSpec]:
    return [
        FieldSpec("cts_number", "property.parcel_identifier", ex.find_cts_number,
                  select=Select.DISTINCT,
                  description="Mumbai urban land key (City Survey)."),
        FieldSpec("survey_number", "property.parcel_identifier", ex.find_survey_number,
                  select=Select.DISTINCT,
                  description="Rural/other Maharashtra parcel key."),
        FieldSpec("plot_number", "property.parcel_identifier", ex.find_plot_number,
                  select=Select.DISTINCT),
        FieldSpec("area", "property.area", ex.find_area, select=Select.DISTINCT,
                  description="Area with unit and measurement basis."),
    ]


def _registration_fields() -> list[FieldSpec]:
    return [
        FieldSpec("registration_number", "registration.number",
                  ex.find_registration_number, pages=PageScope.LAST),
        FieldSpec("sub_registrar", "registration.sub_registrar",
                  ex.find_sub_registrar, pages=PageScope.LAST),
    ]


# =====================================================================================
# Schemas
# =====================================================================================

SALE_DEED_SCHEMA = DocumentSchema(
    document_type=DocumentType.SALE_DEED,
    fields=[
        FieldSpec("seller", "party.seller", ex.find_seller, required=True,
                  select=Select.ALL, pages=PageScope.FIRST),
        FieldSpec("buyer", "party.buyer", ex.find_buyer, required=True,
                  select=Select.ALL, pages=PageScope.FIRST),
        # A Sale Deed transfers title, so its buyer is also an owner claim. An Agreement
        # of Sale deliberately has no such field - TPA s.54.
        FieldSpec("owner", "party.owner", ex.find_buyer, select=Select.ALL,
                  pages=PageScope.FIRST,
                  description="Transferee under a title-transferring instrument."),
        FieldSpec("consideration", "transaction.consideration",
                  ex.find_consideration_amount, required=True, select=Select.DISTINCT),
        FieldSpec("execution_date", "transaction.execution_date",
                  ex.find_execution_date, required=True, pages=PageScope.FIRST),
        *_property_identity_fields(),
        *_registration_fields(),
    ],
)

AGREEMENT_OF_SALE_SCHEMA = DocumentSchema(
    document_type=DocumentType.AGREEMENT_OF_SALE,
    fields=[
        FieldSpec("seller", "party.seller", ex.find_seller, required=True,
                  select=Select.ALL, pages=PageScope.FIRST),
        FieldSpec("buyer", "party.buyer", ex.find_buyer, required=True,
                  select=Select.ALL, pages=PageScope.FIRST),
        # NO owner field. REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST: a contract for sale
        # creates no interest in the property, so an allottee is a prospective purchaser,
        # not an owner. Emitting an ownership claim here would let a case holding only an
        # Agreement of Sale appear to establish title.
        FieldSpec("consideration", "transaction.consideration",
                  ex.find_consideration_amount, required=True, select=Select.DISTINCT),
        FieldSpec("agreement_date", "transaction.agreement_date",
                  ex.find_execution_date, required=True, pages=PageScope.FIRST),
        FieldSpec("maharera_number", "project.rera_registration_number",
                  ex.find_maharera_number, select=Select.DISTINCT),
        *_property_identity_fields(),
        *_registration_fields(),
    ],
)

PROPERTY_TAX_SCHEMA = DocumentSchema(
    document_type=DocumentType.PROPERTY_TAX,
    fields=[
        FieldSpec("assessment_number", "tax.assessment_number",
                  ex.find_assessment_number, required=True, select=Select.DISTINCT),
        FieldSpec("assessee", "party.assessee", ex.find_parties, select=Select.ALL),
        FieldSpec("amount", "tax.amount", ex.find_consideration, select=Select.DISTINCT,
                  description="Any amount on the bill; not necessarily the balance due."),
        FieldSpec("bill_date", "tax.bill_date", ex.find_dates),
        *_property_identity_fields(),
    ],
)

POSSESSION_SCHEMA = DocumentSchema(
    document_type=DocumentType.POSSESSION_DOCUMENT,
    fields=[
        FieldSpec("possession_date", "possession.date", ex.find_execution_date,
                  required=True),
        FieldSpec("recipient", "party.buyer", ex.find_parties, select=Select.ALL),
        *_property_identity_fields(),
    ],
)

MORTGAGE_DEED_SCHEMA = DocumentSchema(
    document_type=DocumentType.MORTGAGE_DEED,
    fields=[
        FieldSpec("mortgagor", "party.mortgagor", ex.find_seller, select=Select.ALL,
                  pages=PageScope.FIRST),
        FieldSpec("mortgagee", "party.mortgagee", ex.find_buyer, select=Select.ALL,
                  pages=PageScope.FIRST),
        FieldSpec("secured_amount", "security.amount", ex.find_consideration_amount,
                  select=Select.DISTINCT),
        FieldSpec("execution_date", "security.execution_date", ex.find_execution_date,
                  pages=PageScope.FIRST),
        *_property_identity_fields(),
        *_registration_fields(),
    ],
)

#: PROPERTY_PAPERS has no schema on purpose. It is a catch-all label for a bundle, not a
#: recognisable document, and the classifier never assigns it. Giving it a schema would
#: let unrecognised content acquire one.
SCHEMAS: dict[DocumentType, DocumentSchema] = {
    DocumentType.SALE_DEED: SALE_DEED_SCHEMA,
    DocumentType.AGREEMENT_OF_SALE: AGREEMENT_OF_SALE_SCHEMA,
    DocumentType.PROPERTY_TAX: PROPERTY_TAX_SCHEMA,
    DocumentType.POSSESSION_DOCUMENT: POSSESSION_SCHEMA,
    DocumentType.MORTGAGE_DEED: MORTGAGE_DEED_SCHEMA,
}


def schema_for(document_type: DocumentType) -> DocumentSchema | None:
    return SCHEMAS.get(document_type)


def supported_types() -> set[DocumentType]:
    return set(SCHEMAS)
