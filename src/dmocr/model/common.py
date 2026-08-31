"""Shared enums and normalised value objects for the canonical model.

Design rules enforced here:

* **No floats for money or area.** Money is integer paise; area is Decimal square metres.
  Float rounding in a consideration amount or a plot area is a correctness bug that would
  surface as a spurious cross-document mismatch.
* **Every value keeps what was written as well as what it means.** `Area` holds the
  original figure and unit alongside the canonical square metres, because a Risk Manager
  comparing "2400 sq.ft" against a normalised 222.967 m2 needs to see both.
* **Determination is five-valued.** Nothing in this system is allowed to collapse
  "we could not tell" into "fail".
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =====================================================================================
# Determinations and status
# =====================================================================================


class Determination(StrEnum):
    """Outcome of any comparison or check.

    Deliberately five-valued. `NOT_DETERMINABLE` and `NOT_APPLICABLE` are distinct and
    neither is a failure: the first means we lacked evidence, the second means the
    question does not arise. Collapsing either into MISMATCH is the single most likely
    source of false positives in this platform.
    """

    MATCH = "MATCH"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_DETERMINABLE = "NOT_DETERMINABLE"

    @property
    def is_adverse(self) -> bool:
        """Only MISMATCH and MISSING count against a case."""
        return self in (Determination.MISMATCH, Determination.MISSING)


class ConfidenceTier(StrEnum):
    """Qualitative confidence.

    Numeric confidence is deliberately NOT the primary representation. Until calibration
    data from real reviewer outcomes exists, a number like 0.94 implies a precision we
    cannot justify, and reviewers will trust it. Tiers first; calibrated numbers later.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFORMATIONAL = "INFORMATIONAL"


# =====================================================================================
# Documents
# =====================================================================================


class DocumentType(StrEnum):
    """MVP document types plus the extension points named in the brief."""

    # MVP five
    AGREEMENT_OF_SALE = "agreement_of_sale"
    SALE_DEED = "sale_deed"
    PROPERTY_PAPERS = "property_papers"
    PROPERTY_TAX = "property_tax"
    POSSESSION_DOCUMENT = "possession_document"

    # Extension points - declared but not implemented in MVP
    ENCUMBRANCE_CERTIFICATE = "encumbrance_certificate"
    MUTATION_RECORD = "mutation_record"
    PROPERTY_CARD = "property_card"
    TITLE_DEED = "title_deed"
    NOC = "noc"
    MORTGAGE_DEED = "mortgage_deed"
    REGISTRATION_RECEIPT = "registration_receipt"
    BUILDING_APPROVAL = "building_approval"
    OCCUPANCY_CERTIFICATE = "occupancy_certificate"
    VALUATION_REPORT = "valuation_report"

    UNKNOWN = "unknown"


class InstrumentStrength(StrEnum):
    """What a document is capable of establishing about ownership.

    Grounded in REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST: an Agreement of Sale is a
    contract and "does not, of itself, create any interest in or charge on such property".
    Without this distinction a case holding only an Agreement of Sale would appear to
    establish title, because the buyer's name would sit in the same `owner` slot as a
    Sale Deed's transferee.
    """

    #: Operates to transfer or declare title (Sale Deed, registered title instrument).
    TITLE_TRANSFERRING = "title_transferring"
    #: Evidences a contract to transfer in future. NOT evidence of ownership.
    CONTRACTUAL = "contractual"
    #: Corroborates possession or occupation, not title.
    POSSESSORY = "possessory"
    #: Government/municipal record naming an assessee or holder. Strong corroboration,
    #: but a tax assessee is not necessarily the legal owner.
    ADMINISTRATIVE = "administrative"
    #: Cannot speak to ownership at all.
    NON_PROBATIVE = "non_probative"


#: Which instrument strength each MVP document type carries.
#: Anything absent is treated as NON_PROBATIVE rather than assumed.
DOCUMENT_INSTRUMENT_STRENGTH: Final[dict[DocumentType, InstrumentStrength]] = {
    DocumentType.SALE_DEED: InstrumentStrength.TITLE_TRANSFERRING,
    DocumentType.TITLE_DEED: InstrumentStrength.TITLE_TRANSFERRING,
    DocumentType.MUTATION_RECORD: InstrumentStrength.ADMINISTRATIVE,
    DocumentType.PROPERTY_CARD: InstrumentStrength.ADMINISTRATIVE,
    DocumentType.PROPERTY_TAX: InstrumentStrength.ADMINISTRATIVE,
    DocumentType.AGREEMENT_OF_SALE: InstrumentStrength.CONTRACTUAL,
    DocumentType.POSSESSION_DOCUMENT: InstrumentStrength.POSSESSORY,
    DocumentType.PROPERTY_PAPERS: InstrumentStrength.NON_PROBATIVE,
    DocumentType.UNKNOWN: InstrumentStrength.NON_PROBATIVE,
}


def instrument_strength_of(doc_type: DocumentType) -> InstrumentStrength:
    return DOCUMENT_INSTRUMENT_STRENGTH.get(doc_type, InstrumentStrength.NON_PROBATIVE)


# =====================================================================================
# Security / mortgage
# =====================================================================================


class SecurityType(StrEnum):
    """Mortgage types under TPA s.58.

    Present in the model because REQ_TPA_59 makes the registration requirement
    *conditional* on this value. In Mumbai the common case is
    `EQUITABLE_DEPOSIT_OF_TITLE_DEEDS`, which s.59 expressly exempts from the
    registered-instrument requirement. A rule that ignores this distinction would flag a
    large share of sound Mumbai cases.
    """

    EQUITABLE_DEPOSIT_OF_TITLE_DEEDS = "equitable_deposit_of_title_deeds"  # s.58(f)
    SIMPLE = "simple"                                                      # s.58(b)
    CONDITIONAL_SALE = "conditional_sale"                                  # s.58(c)
    USUFRUCTUARY = "usufructuary"                                          # s.58(d)
    ENGLISH = "english"                                                    # s.58(e)
    ANOMALOUS = "anomalous"                                                # s.58(g)
    UNKNOWN = "unknown"

    @property
    def requires_registered_instrument(self) -> bool:
        """TPA s.59: all mortgages of Rs.100+ EXCEPT deposit of title-deeds.

        Returns False for UNKNOWN: we must not assert a registration defect when we do
        not yet know the security type. That is a NOT_DETERMINABLE, not a failure.
        """
        return self not in (
            SecurityType.EQUITABLE_DEPOSIT_OF_TITLE_DEEDS,
            SecurityType.UNKNOWN,
        )


# =====================================================================================
# Parcel identifiers
# =====================================================================================


class ParcelIdentifierType(StrEnum):
    """How a parcel is identified.

    Typed rather than a single `survey_number` string, because Mumbai urban land is keyed
    by **CTS number** on the Property Card, while rural Maharashtra uses survey / gat /
    hissa numbers. Flattening these to "survey number" would break on the first
    non-Mumbai district and would silently compare incomparable identifiers.
    """

    CTS = "cts"                # City Survey - Mumbai urban
    SURVEY = "survey"
    GAT = "gat"
    HISSA = "hissa"
    PLOT = "plot"
    FINAL_PLOT = "final_plot"  # Town Planning schemes
    KHASRA = "khasra"
    UNKNOWN = "unknown"


class ParcelIdentifier(BaseModel):
    """A single parcel key, with its type retained."""

    model_config = ConfigDict(frozen=True)

    id_type: ParcelIdentifierType
    value: str = Field(min_length=1)
    #: Sub-registrar office / village / city survey office the key is scoped to. Two
    #: identical CTS numbers in different villages are different parcels.
    locality: str | None = None

    @field_validator("value")
    @classmethod
    def _normalise(cls, v: str) -> str:
        # Collapse whitespace and unify separators; keep the original case since some
        # identifiers carry meaningful suffixes (e.g. "1234/5A").
        return re.sub(r"\s+", "", v).strip("-/.")

    def comparable_key(self) -> tuple[str, str, str]:
        """Key for equality across documents.

        Identifiers of different types are never equal, even if their digits match -
        CTS 145 and Survey 145 are unrelated parcels.
        """
        return (self.id_type.value, self.value.upper(), (self.locality or "").upper())


# =====================================================================================
# Money
# =====================================================================================

_PAISE_PER_RUPEE: Final = 100


class Money(BaseModel):
    """Integer paise. Never a float.

    Consideration amounts drive LTV checks (REQ_HFC_19_1_LTV_COMPUTATION) and the
    Annex XIV 1.9 cap. Binary floating point cannot represent 0.1 exactly, so repeated
    float arithmetic on rupee values produces drift that would surface as a spurious
    cross-document mismatch.
    """

    model_config = ConfigDict(frozen=True)

    paise: int = Field(ge=0)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")

    @classmethod
    def from_rupees(cls, rupees: Decimal | int | str) -> "Money":
        d = Decimal(str(rupees))
        return cls(paise=int((d * _PAISE_PER_RUPEE).to_integral_value()))

    @property
    def rupees(self) -> Decimal:
        return Decimal(self.paise) / _PAISE_PER_RUPEE

    def __str__(self) -> str:
        return f"{self.currency} {format_indian(self.rupees)}"


def format_indian(amount: Decimal) -> str:
    """Format with Indian digit grouping: 1,25,00,000.00, not 12,500,000.00.

    Python's `:,` gives Western thousands grouping. Displaying an Indian lending amount
    that way is a small thing that reads as wrong to anyone who works with these figures
    daily - and we already go to some trouble to PARSE Indian grouping, so showing it
    back in the other convention is inconsistent as well as jarring.

    Last group is three digits, everything above it in twos.
    """
    quantised = amount.quantize(Decimal("0.01"))
    sign = "-" if quantised < 0 else ""
    whole, _, frac = f"{abs(quantised):.2f}".partition(".")

    if len(whole) <= 3:
        return f"{sign}{whole}.{frac}"

    last3, rest = whole[-3:], whole[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return f"{sign}{','.join([*groups, last3])}.{frac}"


# =====================================================================================
# Area
# =====================================================================================


class AreaUnit(StrEnum):
    SQ_FT = "sq_ft"
    SQ_M = "sq_m"
    SQ_YARD = "sq_yard"   # gaj
    GUNTHA = "guntha"     # Maharashtra
    ACRE = "acre"
    HECTARE = "hectare"


#: Exact conversion factors to square metres. Decimal, not float.
_TO_SQ_M: Final[dict[AreaUnit, Decimal]] = {
    AreaUnit.SQ_M: Decimal("1"),
    AreaUnit.SQ_FT: Decimal("0.09290304"),        # exact by definition of the foot
    AreaUnit.SQ_YARD: Decimal("0.83612736"),      # exact
    AreaUnit.ACRE: Decimal("4046.8564224"),       # exact
    AreaUnit.HECTARE: Decimal("10000"),
    AreaUnit.GUNTHA: Decimal("101.17141056"),     # 1/40 acre
}


class Area(BaseModel):
    """An area as written, plus its canonical square-metre value.

    Both are kept. The reviewer needs to see "2400 sq.ft" as it appeared in the deed;
    the comparison engine needs a single canonical unit to compare against a Property
    Card recorded in square metres.
    """

    model_config = ConfigDict(frozen=True)

    value: Decimal = Field(gt=0)
    unit: AreaUnit
    sq_m: Decimal = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def _derive_sq_m(cls, data):
        if isinstance(data, dict) and "sq_m" not in data and "value" in data and "unit" in data:
            unit = AreaUnit(data["unit"])
            value = Decimal(str(data["value"]))
            data = {**data, "sq_m": (value * _TO_SQ_M[unit]).quantize(Decimal("0.000001"))}
        return data

    @classmethod
    def of(cls, value: Decimal | int | float | str, unit: AreaUnit) -> "Area":
        return cls(value=Decimal(str(value)), unit=unit)

    def matches(self, other: "Area", tolerance_pct: Decimal = Decimal("2")) -> bool:
        """Compare on canonical square metres within a tolerance.

        A tolerance is required, not optional: deeds routinely round, and carpet vs
        built-up vs super built-up areas differ legitimately. The default of 2% is a
        starting point to be tuned against reviewer outcomes, not a derived constant.
        """
        if self.sq_m == other.sq_m:
            return True
        larger = max(self.sq_m, other.sq_m)
        diff_pct = abs(self.sq_m - other.sq_m) / larger * 100
        return diff_pct <= tolerance_pct

    def __str__(self) -> str:
        return f"{self.value.normalize()} {self.unit.value} ({self.sq_m.normalize()} m2)"
