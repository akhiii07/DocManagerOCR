"""Case assembly: per-document extractions -> canonical entities.

This is where cross-document validation actually becomes possible. Until claims from
several documents sit on the *same* entity, there is nothing to compare.

TWO RESOLUTION DECISIONS, AND WHY
---------------------------------

**Property: one canonical property per case, by default.**
A case is a loan against one collateral property, so claims from every document attach to
a single `Property`. Documents that assert *different* parcel identifiers therefore produce
competing claims on the same attribute — which resolves to `MISMATCH` through the existing
claim machinery and surfaces as a finding.

The alternative — splitting into separate properties when identifiers disagree — would be
worse: the disagreement would vanish into two tidy entities that never get compared, and
the case would look clean. A conflict must stay visible.

**Parties: grouped by canonical attribute, then by name similarity.**
The seller in the Agreement of Sale and the vendor in the Sale Deed should be one party if
the names agree. Where the match is only partial, they are kept **separate** and the
decision is recorded, because merging on a guess can make a broken title chain look
continuous — the more dangerous error.

Every resolution decision is recorded in `AssemblyResult.decisions` so a reviewer can see
why two names were treated as one person, or not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..extract.service import ExtractionResult, ExtractionService
from ..model.case import Case
from ..model.claims import Claim, TextValue
from ..model.common import Determination
from ..model.entities import Party, Property
from .names import MATCH_THRESHOLD, NameMatch, match_names

log = logging.getLogger(__name__)

#: Attributes that describe a person or organisation rather than the property.
PARTY_ATTRIBUTES = {
    "party.seller", "party.buyer", "party.owner", "party.assessee",
    "party.mortgagor", "party.mortgagee",
}


@dataclass
class ResolutionDecision:
    """One entity-resolution judgement, kept for audit."""

    attribute: str
    left: str
    right: str
    determination: Determination
    score: float
    action: str  # "merged" | "kept_separate"
    reason: str

    def __str__(self) -> str:
        return (f"{self.attribute}: {self.left!r} vs {self.right!r} -> "
                f"{self.determination.value} ({self.score:.2f}) {self.action}")


@dataclass
class AssemblyResult:
    property: Property
    parties: list[Party] = field(default_factory=list)
    claims_added: int = 0
    documents_used: list[str] = field(default_factory=list)
    documents_skipped: list[str] = field(default_factory=list)
    decisions: list[ResolutionDecision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_identity_review(self) -> bool:
        """True when any party resolution landed in the uncertain band."""
        return any(d.determination is Determination.PARTIAL_MATCH for d in self.decisions)

    def parties_for(self, attribute: str) -> list[Party]:
        return [p for p in self.parties if attribute in p.roles]


class CaseAssembler:
    def __init__(self, *, match_threshold: float = MATCH_THRESHOLD):
        self.match_threshold = match_threshold

    def assemble(
        self,
        case: Case,
        extractions: dict[str, ExtractionResult],
    ) -> AssemblyResult:
        """Attach every document's grounded claims to the case's canonical entities."""
        prop = case.properties[0] if case.properties else Property()
        if prop not in case.properties:
            case.properties.append(prop)

        result = AssemblyResult(property=prop)

        for document_id, extraction in sorted(extractions.items()):
            if not extraction.fields:
                result.documents_skipped.append(document_id)
                continue
            result.documents_used.append(document_id)

            claims = ExtractionService.to_claims(
                extraction, subject_id=prop.property_id
            )
            for claim in claims:
                # Party claims attach to BOTH the property and a resolved Party.
                #
                # "Who owns this property" is a fact about the PROPERTY, and that is
                # where cross-document checks look for it. The Party entity exists to
                # group name variants for identity resolution, and needs the claim too so
                # its own claim set is meaningful.
                #
                # Attaching only to the Party was a real bug: ownership checks resolve
                # against the property, so a case WITH a Sale Deed reported "no instrument
                # capable of transferring title names an owner".
                prop.add_claim(claim)
                if claim.attribute in PARTY_ATTRIBUTES:
                    self._attach_party_claim(case, result, claim)
                result.claims_added += 1

        result.parties = list(case.parties)
        self._add_notes(result, extractions)
        return result

    # -- parties -----------------------------------------------------------------

    def _attach_party_claim(
        self, case: Case, result: AssemblyResult, claim: Claim
    ) -> None:
        """Attach a party claim to an existing Party, or create a new one."""
        name = self._claim_name(claim)
        if not name:
            return

        candidates = [p for p in case.parties if claim.attribute in p.roles]
        chosen: Party | None = None

        for party in candidates:
            best: NameMatch | None = None
            best_variant = ""
            for variant in party.name_variants:
                m = match_names(name, variant)
                if best is None or m.score > best.score:
                    best, best_variant = m, variant
            if best is None:
                continue

            if best.determination is Determination.MATCH and best.score >= self.match_threshold:
                chosen = party
                result.decisions.append(ResolutionDecision(
                    attribute=claim.attribute, left=name, right=best_variant,
                    determination=best.determination, score=best.score,
                    action="merged", reason=best.reason,
                ))
                break
            if best.determination is Determination.PARTIAL_MATCH:
                # Kept separate on purpose. Merging on a guess can make a broken title
                # chain look continuous, which is the more dangerous error.
                result.decisions.append(ResolutionDecision(
                    attribute=claim.attribute, left=name, right=best_variant,
                    determination=best.determination, score=best.score,
                    action="kept_separate", reason=best.reason,
                ))

        if chosen is None:
            chosen = Party()
            chosen.roles.add(claim.attribute)
            case.parties.append(chosen)

        chosen.add_name_variant(name)
        # Re-subject the claim onto the party it belongs to. Claims are immutable, so
        # this is a copy rather than a mutation.
        chosen.add_claim(claim.model_copy(update={"subject_id": chosen.party_id}))

    @staticmethod
    def _claim_name(claim: Claim) -> str:
        value = claim.value
        if isinstance(value, TextValue):
            return (value.normalised or value.raw or "").strip()
        return ""

    # -- notes -------------------------------------------------------------------

    @staticmethod
    def _add_notes(result: AssemblyResult, extractions: dict[str, ExtractionResult]) -> None:
        if len(result.documents_used) < 2:
            result.notes.append(
                "Fewer than two documents contributed claims, so cross-document "
                "agreement cannot be established. A lone assertion is not corroboration."
            )
        for document_id, extraction in extractions.items():
            if extraction.missing_required:
                result.notes.append(
                    f"{document_id}: required fields not extracted: "
                    f"{', '.join(extraction.missing_required)}"
                )
            if extraction.rejected_ungrounded:
                result.notes.append(
                    f"{document_id}: {len(extraction.rejected_ungrounded)} candidate "
                    f"value(s) discarded because they could not be located on the page."
                )
