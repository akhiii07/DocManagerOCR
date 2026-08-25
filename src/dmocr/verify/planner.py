"""Verification planner.

Turns a case into a plan naming which authorities are in scope, at what access tier, and
**what minimum data to send each one**.

This is a distinct component rather than logic inside adapters because source selection is
an inference — "not every property maps to every source" — and inferences deserve their own
tests. Hard-coding selection into adapters would make it untestable and would bury the
jurisdiction rules that change most often as states are added.

DATA MINIMISATION IS PART OF PLANNING, NOT AN AFTERTHOUGHT
----------------------------------------------------------
An external lookup is an **outbound disclosure of customer data**, not a neutral read.
The planner picks the narrowest identifier that resolves a record: if a CTS number alone
retrieves a Property Card, the owner's name is not sent. What was sent is recorded on the
snapshot so the disclosure is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..model.case import Case
from ..model.claims import AreaValue, ClaimValue, MoneyValue, ParcelValue, TextValue
from ..model.common import Determination, ParcelIdentifierType
from ..model.entities import Property
from .results import AccessTier
from .sources import SourceSpec, default_sources


class Execution(StrEnum):
    AUTOMATED = "AUTOMATED"
    #: T4/T5, or an automatable tier that is blocked on an unresolved question.
    MANUAL = "MANUAL"
    #: Out of scope for this case. Reported, not silently dropped.
    SKIP = "SKIP"


#: Lookup-key vocabulary from the research file -> canonical model attribute.
_KEY_TO_ATTRIBUTE: dict[str, str] = {
    "cts_number": "property.parcel_identifier",
    "property_identifiers": "property.parcel_identifier",
    "maharera_registration_number": "project.rera_registration_number",
    "project_name": "project.name",
    "promoter_name": "project.promoter",
    "assessment_number": "tax.assessment_number",
    "property_account_number_pid": "tax.assessment_number",
    "document_number": "registration.number",
    "borrower_identifiers": "party.owner",
    "ward_name_address": "property.address",
    "city_survey_office": "property.locality",
    "sro": "registration.sub_registrar",
}


@dataclass
class PlanItem:
    source: SourceSpec
    execution: Execution
    #: Attributes this source will be asked about, limited to ones we hold internally.
    attributes: list[str] = field(default_factory=list)
    #: The minimum identifiers to send. Empty when nothing usable was extracted.
    lookup_keys: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    #: Set when a key we would send is itself disputed across documents.
    ambiguous_keys: list[str] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def tier(self) -> AccessTier:
        return self.source.tier

    @property
    def is_actionable(self) -> bool:
        return self.execution is not Execution.SKIP and bool(self.lookup_keys)


@dataclass
class VerificationPlan:
    case_id: str
    items: list[PlanItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def automated(self) -> list[PlanItem]:
        return [i for i in self.items if i.execution is Execution.AUTOMATED]

    @property
    def manual(self) -> list[PlanItem]:
        return [i for i in self.items if i.execution is Execution.MANUAL]

    @property
    def skipped(self) -> list[PlanItem]:
        return [i for i in self.items if i.execution is Execution.SKIP]

    def summary(self) -> dict[str, int]:
        return {
            "sources_considered": len(self.items),
            "automated": len(self.automated),
            "manual": len(self.manual),
            "skipped": len(self.skipped),
            "actionable": sum(1 for i in self.items if i.is_actionable),
        }


class VerificationPlanner:
    """Decides which authorities are in scope for a case."""

    #: MVP jurisdiction. Every registered source is Maharashtra-specific.
    SUPPORTED_STATES = {"MH"}

    def __init__(self, sources: dict[str, SourceSpec] | None = None):
        self.sources = sources if sources is not None else default_sources()

    def plan(self, case: Case) -> VerificationPlan:
        plan = VerificationPlan(case_id=case.case_id)

        if case.state not in self.SUPPORTED_STATES:
            plan.notes.append(
                f"No verification sources registered for state {case.state!r}. "
                f"MVP covers Maharashtra only; this is NOT_APPLICABLE, not a failure."
            )
            return plan

        prop = case.properties[0] if case.properties else None
        if prop is None:
            plan.notes.append("No property on the case; nothing to verify.")
            return plan

        for source in sorted(self.sources.values(), key=lambda s: s.priority):
            plan.items.append(self._plan_source(case, prop, source))
        return plan

    # -- per source --------------------------------------------------------------

    def _plan_source(self, case: Case, prop: Property, source: SourceSpec) -> PlanItem:
        gate_reason = self._applicability_gate(source, prop)
        if gate_reason:
            return PlanItem(source=source, execution=Execution.SKIP, reason=gate_reason)

        keys, ambiguous = self._minimal_keys(prop, source)
        if not keys:
            return PlanItem(
                source=source, execution=Execution.SKIP,
                ambiguous_keys=ambiguous,
                reason=(
                    f"No usable lookup key extracted. This source is keyed by "
                    f"{list(source.keyed_by)}, none of which is present."
                ),
            )

        attributes = [a for a in source.verifies if a in prop.claim_sets] or list(
            source.verifies
        )

        if source.blocked_on:
            return PlanItem(
                source=source, execution=Execution.MANUAL, attributes=attributes,
                lookup_keys=keys, ambiguous_keys=ambiguous,
                reason=(
                    f"Automation blocked pending: {', '.join(source.blocked_on)}. "
                    f"Routed to a human operator."
                ),
            )

        execution = (
            Execution.AUTOMATED if source.is_automatable else Execution.MANUAL
        )
        reason = (
            f"Tier {source.tier.value}"
            + (f" ({source.tier_confidence.lower()} confidence)"
               if source.tier_confidence != "UNKNOWN" else "")
            + ("; human-operated." if execution is Execution.MANUAL else "; automatable.")
        )
        return PlanItem(source=source, execution=execution, attributes=attributes,
                        lookup_keys=keys, ambiguous_keys=ambiguous, reason=reason)

    def _applicability_gate(self, source: SourceSpec, prop: Property) -> str | None:
        """Whether the source's question arises for this property at all."""
        if source.source_id == "SRC_MAHARERA":
            # REQ_RERA_3_2_REGISTRATION_EXEMPTION is REQUIRES_LEGAL_REVIEW, and much of
            # Mumbai's older resale stock has no RERA record at all. Absent a registration
            # number, the correct answer is NOT_APPLICABLE - never a finding.
            resolution = prop.resolve("project.rera_registration_number")
            if resolution.determination is Determination.MISSING:
                return (
                    "No MahaRERA registration number extracted. RERA registration is "
                    "subject to the s.3(2) exemptions, which remain REQUIRES_LEGAL_REVIEW, "
                    "so absence defaults to NOT_APPLICABLE rather than a finding."
                )

        if source.source_id == "SRC_PROPERTY_CARD_MH":
            cts = [
                p for p in prop.parcel_identifiers
                if p.id_type is ParcelIdentifierType.CTS
            ]
            resolution = prop.resolve("property.parcel_identifier")
            if not cts and resolution.determination is Determination.MISSING:
                return (
                    "No CTS number extracted. The Mumbai Property Card is keyed by CTS "
                    "number; without one the record cannot be located."
                )
        return None

    @staticmethod
    def _key_value(value: ClaimValue) -> str:
        """Render a claim value as a lookup key an operator or API can actually use.

        `comparable()` returns a tuple for parcel identifiers, which is right for equality
        but wrong to put in front of a person: an operator handed "('cts', '1234/5A', '')"
        cannot type that into a portal.
        """
        if isinstance(value, ParcelValue):
            return value.identifier.value
        if isinstance(value, TextValue):
            return value.raw
        if isinstance(value, MoneyValue):
            return str(value.amount.rupees)
        if isinstance(value, AreaValue):
            return str(value.area.value)
        return str(value.comparable())

    @classmethod
    def _minimal_keys(cls, prop: Property, source: SourceSpec) -> tuple[dict[str, str], list[str]]:
        """The narrowest identifier that resolves a record, plus any disputed keys.

        `keyed_by` is ordered best-first in the research file, so the first key we hold is
        the most specific one available - and once we have it, nothing further is sent.
        """
        ambiguous: list[str] = []
        for key in source.keyed_by:
            attribute = _KEY_TO_ATTRIBUTE.get(key)
            if attribute is None:
                continue
            resolution = prop.resolve(attribute)
            if resolution.determination is Determination.MISSING or resolution.value is None:
                continue
            if resolution.determination is Determination.MISMATCH:
                # Querying on a disputed identifier would retrieve the wrong record and
                # produce a confidently wrong verification. Record it and keep looking.
                ambiguous.append(key)
                continue
            return {key: cls._key_value(resolution.value)}, ambiguous
        return {}, ambiguous
