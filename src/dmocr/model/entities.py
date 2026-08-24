"""Canonical entities: Party, Property, Project.

These carry **identity and claim sets**, not fields. A `Property` does not have an `area`
attribute; it has a claim set for `property.area` that may contain several disagreeing
assertions. Asking for "the area" is a resolution operation with an explicit
determination, not an attribute read.

That shape is what allows the review package to say "MISMATCH: Sale Deed 2400 sq ft vs Tax
receipt 2210 sq ft" instead of silently reporting whichever was written last.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .claims import Claim, ClaimSet, Resolution
from .common import Determination, ParcelIdentifier


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ClaimHolder(BaseModel):
    """Mixin for entities that accumulate claims, keyed by attribute."""

    model_config = ConfigDict(frozen=False)

    claim_sets: dict[str, ClaimSet] = Field(default_factory=dict)

    def add_claim(self, claim: Claim) -> None:
        cs = self.claim_sets.get(claim.attribute)
        if cs is None:
            cs = ClaimSet(subject_id=claim.subject_id, attribute=claim.attribute)
        self.claim_sets[claim.attribute] = cs.with_claim(claim)

    def add_claims(self, claims: Iterable[Claim]) -> None:
        for c in claims:
            self.add_claim(c)

    def resolve(self, attribute: str, **kwargs) -> Resolution:
        cs = self.claim_sets.get(attribute)
        if cs is None:
            return Resolution(
                attribute=attribute,
                determination=Determination.MISSING,
                rationale="No claims recorded for this attribute.",
            )
        return cs.resolve(**kwargs)

    def all_claims(self) -> list[Claim]:
        return [c for cs in self.claim_sets.values() for c in cs.claims]


class Party(ClaimHolder):
    """A person or organisation appearing in the documents.

    Name matching is deliberately NOT string equality. Indian names carry transliteration
    variants, initials, patronymics, honorifics and inconsistent ordering, so
    `name_variants` records every surface form encountered and matching is a scored
    operation handled elsewhere. Collapsing them here would destroy the evidence that a
    match was uncertain.
    """

    party_id: str = Field(default_factory=lambda: _new_id("PTY"))
    #: Every surface form seen, with the document each came from. Never deduplicated by
    #: casual normalisation.
    name_variants: list[str] = Field(default_factory=list)
    #: Roles this party plays, e.g. {"seller"}, {"buyer", "mortgagor"}. A party can hold
    #: several roles across a document bundle.
    roles: set[str] = Field(default_factory=set)

    def add_name_variant(self, name: str) -> None:
        if name and name not in self.name_variants:
            self.name_variants.append(name)


class Project(ClaimHolder):
    """A real-estate project, where one applies.

    Phase is a first-class field because RERA s.3 Explanation makes every phase a
    standalone project with its own registration number
    (`REQ_RERA_3_EXPLANATION_PHASE_IS_STANDALONE`). Comparing "ABC Residency" against
    "ABC Residency Phase II" by name is therefore not a mismatch test - it is a category
    error. Match on registration number wherever one is available.
    """

    project_id: str = Field(default_factory=lambda: _new_id("PRJ"))
    name: str | None = None
    phase: str | None = None
    rera_registration_number: str | None = None

    @property
    def rera_applicable(self) -> bool | None:
        """Tri-state on purpose.

        None means "not yet determined". The RERA s.3(2) exemption logic is marked
        REQUIRES_LEGAL_REVIEW (`REQ_RERA_3_2_REGISTRATION_EXEMPTION`), so this must not
        default to True or False - either default would manufacture findings on the large
        share of Mumbai collateral that is older resale stock.
        """
        return None


class Property(ClaimHolder):
    """The collateral property.

    Parcel identifiers are typed and plural: a Mumbai flat may be identified by CTS number
    on the Property Card and by a plot number in the deed, and those are different keys
    that must not be compared to each other.
    """

    property_id: str = Field(default_factory=lambda: _new_id("PRP"))
    parcel_identifiers: list[ParcelIdentifier] = Field(default_factory=list)
    project_id: str | None = None

    def add_parcel_identifier(self, pid: ParcelIdentifier) -> None:
        if pid.comparable_key() not in {p.comparable_key() for p in self.parcel_identifiers}:
            self.parcel_identifiers.append(pid)

    def identifiers_of_type(self, id_type) -> list[ParcelIdentifier]:
        return [p for p in self.parcel_identifiers if p.id_type == id_type]

    def shares_identifier_with(self, other: "Property") -> bool:
        """True if the two describe the same parcel by at least one common typed key.

        Cross-type coincidences do not count: CTS 145 and Survey 145 are unrelated
        parcels that happen to share digits.
        """
        mine = {p.comparable_key() for p in self.parcel_identifiers}
        theirs = {p.comparable_key() for p in other.parcel_identifiers}
        return bool(mine & theirs)
