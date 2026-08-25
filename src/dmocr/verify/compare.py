"""Comparing an external observation against internal claims.

Reuses the same comparison semantics as cross-document validation - area tolerance and
measurement basis, typed parcel keys, scored name matching - so "the deed disagrees with
the tax bill" and "the deed disagrees with the Property Card" are judged the same way.

Two behaviours are specific to external comparison:

**Containment yields PARTIAL_MATCH.** RERA registers each phase as a standalone project
(`REQ_RERA_3_EXPLANATION_PHASE_IS_STANDALONE`), so a deed naming "ABC Residency" against a
RERA record for "ABC Residency Phase II" is the *expected* shape of a phased project, not a
contradiction. Reporting that as MISMATCH would generate a finding on every phased
development.

**The access tier caps confidence.** A statutory API and an operator's screenshot do not
carry the same weight, however clean the comparison looks.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..model.claims import (
    AreaValue,
    ClaimValue,
    MoneyValue,
    ParcelValue,
    Resolution,
    TextValue,
)
from ..model.common import ConfidenceTier, Determination
from ..resolve.names import match_names
from .results import (
    AccessTier,
    ExternalObservation,
    VerificationResult,
    VerificationStatus,
)

#: Attributes compared as people's names rather than as plain text.
NAME_ATTRIBUTES = {
    "party.owner", "party.seller", "party.buyer", "party.assessee",
    "party.mortgagor", "party.mortgagee", "project.promoter",
}

_CONFIDENCE_ORDER = [
    ConfidenceTier.INSUFFICIENT,
    ConfidenceTier.LOW,
    ConfidenceTier.MEDIUM,
    ConfidenceTier.HIGH,
]


def _cap(value: ConfidenceTier, ceiling: ConfidenceTier) -> ConfidenceTier:
    return min(value, ceiling, key=_CONFIDENCE_ORDER.index)


def _describe(value: ClaimValue | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, AreaValue):
        return str(value.area)
    if isinstance(value, MoneyValue):
        return str(value.amount)
    if isinstance(value, ParcelValue):
        ident = value.identifier
        return f"{ident.id_type.value}:{ident.value}"
    if isinstance(value, TextValue):
        return value.raw
    return str(value.comparable())


def compare_values(
    attribute: str,
    internal: ClaimValue,
    external: ClaimValue,
    *,
    area_tolerance_pct: Decimal = Decimal("2"),
) -> tuple[VerificationStatus, str]:
    """Compare one internal value against one external value."""
    if internal.kind != external.kind:
        return (
            VerificationStatus.MISMATCH,
            f"Value types differ ({internal.kind} vs {external.kind}).",
        )

    if attribute in NAME_ATTRIBUTES and isinstance(internal, TextValue):
        m = match_names(internal.raw, external.raw)  # type: ignore[union-attr]
        if m.determination is Determination.MATCH:
            return VerificationStatus.MATCH, m.reason
        if m.determination is Determination.PARTIAL_MATCH:
            return VerificationStatus.PARTIAL_MATCH, m.reason
        if m.determination is Determination.NOT_DETERMINABLE:
            return VerificationStatus.NOT_FOUND_IN_SOURCE, m.reason
        return VerificationStatus.MISMATCH, f"{m.reason} (score {m.score:.2f})"

    if isinstance(internal, AreaValue) and isinstance(external, AreaValue):
        if internal.basis != external.basis and "unspecified" not in (
            internal.basis, external.basis
        ):
            return (
                VerificationStatus.PARTIAL_MATCH,
                f"Areas are on different bases ({internal.basis} vs {external.basis}); "
                f"they are not directly comparable.",
            )
        if internal.area.matches(external.area, area_tolerance_pct):
            return VerificationStatus.MATCH, "Areas agree within tolerance."
        return (
            VerificationStatus.MISMATCH,
            f"{internal.area} vs {external.area}.",
        )

    if isinstance(internal, TextValue) and isinstance(external, TextValue):
        a, b = internal.comparable(), external.comparable()
        if a == b:
            return VerificationStatus.MATCH, "Values agree."
        if a and b and (a in b or b in a):
            # Phase containment, per REQ_RERA_3_EXPLANATION_PHASE_IS_STANDALONE.
            return (
                VerificationStatus.PARTIAL_MATCH,
                f"One value contains the other ({internal.raw!r} vs {external.raw!r}). "
                f"For a RERA project this is the expected shape of a phased development, "
                f"not a contradiction - confirm the phase.",
            )
        return VerificationStatus.MISMATCH, f"{internal.raw!r} vs {external.raw!r}."

    if internal.comparable() == external.comparable():
        return VerificationStatus.MATCH, "Values agree."
    return (
        VerificationStatus.MISMATCH,
        f"{_describe(internal)} vs {_describe(external)}.",
    )


def compare_observation(
    attribute: str,
    internal: Resolution,
    observation: ExternalObservation,
    *,
    tier: AccessTier,
    freshness=None,
    area_tolerance_pct: Decimal = Decimal("2"),
) -> VerificationResult:
    """Compare one attribute of an observation against the internal resolution."""
    snapshot = observation.snapshot
    base = dict(
        source_id=snapshot.source_id,
        authority=snapshot.authority,
        attribute=attribute,
        tier=tier,
        snapshot_id=snapshot.snapshot_id,
        checked_at=snapshot.retrieved_at,
        internal_claim_ids=list(internal.supporting_claim_ids),
        internal_value=_describe(internal.value),
    )

    if not observation.record_found:
        return VerificationResult(
            status=VerificationStatus.NOT_FOUND_IN_SOURCE,
            confidence=_cap(ConfidenceTier.MEDIUM, tier.confidence_ceiling),
            detail="The authority responded and holds no matching record.",
            **base,
        )

    external = observation.fields.get(attribute)
    if external is None:
        return VerificationResult(
            status=VerificationStatus.NOT_APPLICABLE,
            detail=f"The source does not report {attribute}.",
            **base,
        )

    base["external_value"] = _describe(external)

    if internal.determination is Determination.MISSING or internal.value is None:
        return VerificationResult(
            status=VerificationStatus.NOT_APPLICABLE,
            confidence=ConfidenceTier.INSUFFICIENT,
            detail=(
                "Nothing extracted internally to compare against. The external value is "
                "recorded but this is not a check."
            ),
            **base,
        )

    if observation.is_stale(freshness):
        age = snapshot.retrieved_at - (observation.record_as_of or snapshot.retrieved_at)
        return VerificationResult(
            status=VerificationStatus.STALE,
            confidence=ConfidenceTier.LOW,
            detail=f"Record is {age.days} days old, beyond the freshness policy.",
            **base,
        )

    status, detail = compare_values(
        attribute, internal.value, external, area_tolerance_pct=area_tolerance_pct
    )

    # A disputed internal value cannot be confirmed by an external one: agreeing with the
    # majority reading does not resolve the disagreement between our own documents.
    if internal.determination is Determination.MISMATCH and status is VerificationStatus.MATCH:
        status = VerificationStatus.PARTIAL_MATCH
        detail = (
            f"{detail} The internal value is itself disputed across documents, so the "
            f"external record confirms only one of the competing readings."
        )

    confidence = _cap(
        ConfidenceTier.HIGH if status is VerificationStatus.MATCH else ConfidenceTier.MEDIUM,
        tier.confidence_ceiling,
    )
    return VerificationResult(status=status, confidence=confidence, detail=detail, **base)


def unavailable_result(
    source_id: str, authority: str, attribute: str, tier: AccessTier, reason: str
) -> VerificationResult:
    """Build the result for a source we could not reach.

    Kept as a named constructor so the invariant is visible in one place: this status is
    never adverse and never counts as a check.
    """
    return VerificationResult(
        source_id=source_id,
        authority=authority,
        attribute=attribute,
        tier=tier,
        status=VerificationStatus.SOURCE_UNAVAILABLE,
        confidence=ConfidenceTier.INSUFFICIENT,
        detail=f"Source unavailable: {reason}. This is not a compliance failure.",
        checked_at=datetime.now(),
    )
