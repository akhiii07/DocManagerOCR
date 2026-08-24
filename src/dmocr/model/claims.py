"""Claims: the atom of the canonical model.

Per ADR-0003 the model stores *claims*, not resolved fields. Several documents and
external sources may each assert something about the same property attribute, and they may
disagree. That disagreement IS the finding, so it must be representable.

Resolution therefore produces a `Resolution` **view**; it never mutates the claim set and
never discards a losing claim. "The Sale Deed says 2400 sq ft, the tax receipt says 2210,
MahaRERA says 2400" has to survive intact all the way to the review package.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    Area,
    ConfidenceTier,
    Determination,
    InstrumentStrength,
    Money,
    ParcelIdentifier,
)
from .provenance import Provenance


# =====================================================================================
# Claim values
# =====================================================================================


class TextValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["text"] = "text"
    raw: str
    #: Case/whitespace-folded form used for comparison. Names get their own comparison
    #: path (transliteration, initials, honorifics) and must not rely on this.
    normalised: str | None = None

    def comparable(self) -> str:
        return (self.normalised or self.raw).strip().casefold()


class MoneyValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["money"] = "money"
    amount: Money

    def comparable(self) -> int:
        return self.amount.paise


class AreaValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["area"] = "area"
    area: Area
    #: Areas are not one thing. A carpet-area figure and a super-built-up figure for the
    #: same flat legitimately differ by 30%+, so comparing across bases is meaningless.
    basis: Literal["carpet", "built_up", "super_built_up", "plot", "unspecified"] = "unspecified"

    def comparable(self) -> Decimal:
        return self.area.sq_m


class DateValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["date"] = "date"
    value: date

    def comparable(self) -> date:
        return self.value


class ParcelValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["parcel"] = "parcel"
    identifier: ParcelIdentifier

    def comparable(self) -> tuple[str, str, str]:
        return self.identifier.comparable_key()


class BoolValue(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["bool"] = "bool"
    value: bool

    def comparable(self) -> bool:
        return self.value


ClaimValue = Annotated[
    TextValue | MoneyValue | AreaValue | DateValue | ParcelValue | BoolValue,
    Field(discriminator="kind"),
]


# =====================================================================================
# Claim
# =====================================================================================


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Claim(BaseModel):
    """One source asserting one attribute of one subject.

    Immutable. Corrections are new claims carrying `HumanProvenance.supersedes`, never
    edits - an overridden value must remain auditable.
    """

    model_config = ConfigDict(frozen=True)

    claim_id: str = Field(default_factory=lambda: _new_id("CLM"))
    #: Entity this is about (property_id, party_id, project_id, case_id).
    subject_id: str
    #: Dotted attribute path, e.g. "property.area", "party.name", "transaction.consideration".
    attribute: str
    value: ClaimValue
    provenance: Provenance
    confidence: ConfidenceTier = ConfidenceTier.MEDIUM

    #: What the asserting document is capable of establishing. Set from the document type
    #: for document-sourced claims; None for external/human/derived origins.
    instrument_strength: InstrumentStrength | None = None

    #: When the asserted fact was true, if different from when it was recorded. A 2019 tax
    #: receipt asserts the assessee as at 2019, not today.
    as_of: date | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now())

    def can_establish_ownership(self) -> bool:
        """Whether this claim may support an ownership conclusion.

        REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST: an Agreement of Sale evidences a contract
        and creates no interest in the property, so a buyer named in one is a prospective
        purchaser, not an owner.
        """
        return self.instrument_strength in (
            InstrumentStrength.TITLE_TRANSFERRING,
            InstrumentStrength.ADMINISTRATIVE,
        )


# =====================================================================================
# Resolution
# =====================================================================================


class Resolution(BaseModel):
    """A derived view over a claim set. Never stored as truth."""

    model_config = ConfigDict(frozen=True)

    attribute: str
    determination: Determination
    #: The value the system would proceed with, where one can be chosen at all.
    value: ClaimValue | None = None
    supporting_claim_ids: list[str] = Field(default_factory=list)
    conflicting_claim_ids: list[str] = Field(default_factory=list)
    confidence: ConfidenceTier = ConfidenceTier.INSUFFICIENT
    rationale: str = ""


def _agree(a: ClaimValue, b: ClaimValue, area_tolerance_pct: Decimal) -> bool:
    if a.kind != b.kind:
        return False
    if isinstance(a, AreaValue) and isinstance(b, AreaValue):
        # Different measurement bases are not comparable at all.
        if a.basis != b.basis and "unspecified" not in (a.basis, b.basis):
            return False
        return a.area.matches(b.area, area_tolerance_pct)
    return a.comparable() == b.comparable()


class ClaimSet(BaseModel):
    """All claims about one attribute of one subject."""

    model_config = ConfigDict(frozen=True)

    subject_id: str
    attribute: str
    claims: list[Claim] = Field(default_factory=list)

    def with_claim(self, claim: Claim) -> "ClaimSet":
        """Return a new set including `claim`. The model is append-only."""
        if claim.subject_id != self.subject_id or claim.attribute != self.attribute:
            raise ValueError(
                f"claim {claim.claim_id} does not belong to "
                f"{self.subject_id}/{self.attribute}"
            )
        return self.model_copy(update={"claims": [*self.claims, claim]})

    def active_claims(self) -> list[Claim]:
        """Claims not superseded by a human correction."""
        superseded = {
            c.provenance.supersedes
            for c in self.claims
            if getattr(c.provenance, "supersedes", None)
        }
        return [c for c in self.claims if c.claim_id not in superseded]

    def resolve(
        self,
        *,
        area_tolerance_pct: Decimal = Decimal("2"),
        ownership_only: bool = False,
    ) -> Resolution:
        """Produce a view over the claims. Does not mutate anything.

        `ownership_only` restricts to claims capable of establishing ownership, so that
        an Agreement of Sale cannot by itself answer "who owns this?".
        """
        claims = self.active_claims()
        if ownership_only:
            claims = [c for c in claims if c.can_establish_ownership()]

        if not claims:
            return Resolution(
                attribute=self.attribute,
                determination=Determination.MISSING,
                rationale=(
                    "No claim capable of establishing ownership."
                    if ownership_only
                    else "No claims recorded for this attribute."
                ),
            )

        if len(claims) == 1:
            only = claims[0]
            return Resolution(
                attribute=self.attribute,
                determination=Determination.NOT_DETERMINABLE,
                value=only.value,
                supporting_claim_ids=[only.claim_id],
                confidence=ConfidenceTier.LOW,
                rationale=(
                    "Single source; nothing to corroborate against. A lone assertion is "
                    "not agreement."
                ),
            )

        # Group into agreement clusters. Values of different kinds never agree.
        clusters: list[list[Claim]] = []
        for c in claims:
            for cluster in clusters:
                if _agree(cluster[0].value, c.value, area_tolerance_pct):
                    cluster.append(c)
                    break
            else:
                clusters.append([c])

        clusters.sort(key=len, reverse=True)
        largest = clusters[0]

        if len(clusters) == 1:
            return Resolution(
                attribute=self.attribute,
                determination=Determination.MATCH,
                value=largest[0].value,
                supporting_claim_ids=[c.claim_id for c in largest],
                confidence=ConfidenceTier.HIGH,
                rationale=f"{len(largest)} independent sources agree.",
            )

        others = [c.claim_id for cl in clusters[1:] for c in cl]
        return Resolution(
            attribute=self.attribute,
            determination=Determination.MISMATCH,
            # A value is still offered - the reviewer needs somewhere to start - but the
            # determination is MISMATCH and every dissenting claim is carried alongside.
            value=largest[0].value,
            supporting_claim_ids=[c.claim_id for c in largest],
            conflicting_claim_ids=others,
            confidence=ConfidenceTier.INSUFFICIENT,
            rationale=(
                f"{len(clusters)} distinct values across {len(claims)} sources; "
                f"largest agreement group has {len(largest)}. Requires human review."
            ),
        )
