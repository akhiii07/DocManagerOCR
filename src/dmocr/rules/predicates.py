"""Registered predicates.

Each implements one check named by a rule in `rules/*.yaml`. They compute a
`Determination` and gather evidence; they do NOT decide severity or disposition, which are
policy and live in the rule spec.

A recurring pattern here: when an input is absent, the predicate returns
`NOT_DETERMINABLE`, never a failure. "We could not check" and "the check failed" are
different answers, and conflating them is the fastest way to make a reviewer stop trusting
the system.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from ..model.common import ConfidenceTier, Determination, DocumentType, SecurityType
from ..model.findings import Evidence
from ..model.verification import VerificationResult, VerificationStatus
from .registry import PredicateOutcome, predicate

if TYPE_CHECKING:
    from ..model.case import Case


# =====================================================================================
# Document completeness
# =====================================================================================


@predicate("documents_present")
def documents_present(case: "Case", params: dict) -> PredicateOutcome:
    """All expected document types are present in the bundle."""
    expected = case.expected_documents
    if not expected:
        return PredicateOutcome.not_determinable(
            "No expected document list configured for this case."
        )
    missing = case.missing_document_types()
    if not missing:
        return PredicateOutcome(
            determination=Determination.MATCH,
            evidence=Evidence(
                document_ids=[d.document_id for d in case.documents],
                note=f"All {len(expected)} expected document types present.",
            ),
            message_vars={"missing": "", "expected_count": len(expected)},
        )
    names = ", ".join(t.value for t in missing)
    return PredicateOutcome(
        determination=Determination.MISSING,
        evidence=Evidence(
            document_ids=[d.document_id for d in case.documents],
            note=f"Missing: {names}",
        ),
        message_vars={"missing": names, "expected_count": len(expected)},
    )


# =====================================================================================
# Cross-document consistency
# =====================================================================================


@predicate("claim_consistency")
def claim_consistency(case: "Case", params: dict) -> PredicateOutcome:
    """Sources agree on an attribute of the property.

    Delegates to `ClaimSet.resolve`, so the tolerance and measurement-basis rules live in
    one place. A single source resolves to NOT_DETERMINABLE — a lone assertion is not
    agreement, and reporting it as MATCH would manufacture false assurance.
    """
    attribute: str = params["attribute"]
    tolerance = Decimal(str(params.get("tolerance_pct", 2)))

    if not case.properties:
        return PredicateOutcome.not_determinable("No property on the case.", attribute=attribute)

    worst: PredicateOutcome | None = None
    rank = {
        Determination.MISMATCH: 0,
        Determination.MISSING: 1,
        Determination.NOT_DETERMINABLE: 2,
        Determination.PARTIAL_MATCH: 3,
        Determination.MATCH: 4,
        Determination.NOT_APPLICABLE: 5,
    }

    for prop in case.properties:
        res = prop.resolve(attribute, area_tolerance_pct=tolerance)
        out = PredicateOutcome(
            determination=res.determination,
            evidence=Evidence(
                claim_ids=[*res.supporting_claim_ids, *res.conflicting_claim_ids],
                note=res.rationale,
            ),
            message_vars={
                "attribute": attribute,
                "rationale": res.rationale,
                "conflicts": len(res.conflicting_claim_ids),
            },
        )
        if worst is None or rank[out.determination] < rank[worst.determination]:
            worst = out
    return worst  # type: ignore[return-value]


@predicate("ownership_established")
def ownership_established(case: "Case", params: dict) -> PredicateOutcome:
    """A title-transferring instrument names an owner.

    REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST: an Agreement of Sale creates no interest in
    the property, so it cannot answer this. `ownership_only=True` filters to instruments
    capable of establishing title.
    """
    attribute: str = params.get("attribute", "party.owner")
    if not case.properties:
        return PredicateOutcome.not_determinable("No property on the case.")

    for prop in case.properties:
        res = prop.resolve(attribute, ownership_only=True)
        if res.determination is not Determination.MISSING:
            return PredicateOutcome(
                determination=res.determination,
                evidence=Evidence(
                    claim_ids=[*res.supporting_claim_ids, *res.conflicting_claim_ids],
                    note=res.rationale,
                ),
                message_vars={"rationale": res.rationale},
            )

    contractual = [
        d.document_id for d in case.documents
        if d.document_type is DocumentType.AGREEMENT_OF_SALE
    ]
    note = (
        "No instrument capable of transferring title names an owner."
        + (
            f" An Agreement of Sale is present ({len(contractual)}), but under TPA s.54 a "
            f"contract for sale creates no interest in the property."
            if contractual else ""
        )
    )
    return PredicateOutcome(
        determination=Determination.MISSING,
        evidence=Evidence(document_ids=contractual, note=note),
        message_vars={"rationale": note},
    )


# =====================================================================================
# Security and registration - TPA s.59 with the s.58(f) carve-out
# =====================================================================================


@predicate("mortgage_registration_required")
def mortgage_registration_required(case: "Case", params: dict) -> PredicateOutcome:
    """A non-equitable mortgage carries registration particulars.

    The carve-out is the whole point. TPA s.59 excepts mortgage by deposit of title-deeds,
    which under s.58(f) is available in Bombay and is the dominant Mumbai practice. A
    blanket "mortgages must be registered" check would fire on a large share of sound
    cases.
    """
    required = case.mortgage_requires_registration()

    if required is None:
        return PredicateOutcome.not_determinable(
            "Security type not established, so the TPA s.59 registration requirement "
            "cannot be evaluated."
        )
    if required is False:
        return PredicateOutcome.not_applicable(
            f"Security is {case.security_type.value}; TPA s.59 expressly excepts "
            f"mortgage by deposit of title-deeds from the registered-instrument "
            f"requirement."
        )

    deeds = case.documents_of_type(DocumentType.MORTGAGE_DEED)
    if not deeds:
        return PredicateOutcome(
            determination=Determination.MISSING,
            evidence=Evidence(note="No mortgage deed in the bundle."),
            message_vars={"security_type": case.security_type.value},
        )
    return PredicateOutcome(
        determination=Determination.MATCH,
        evidence=Evidence(
            document_ids=[d.document_id for d in deeds],
            note="Mortgage deed present.",
        ),
        message_vars={"security_type": case.security_type.value},
    )


@predicate("originals_held")
def originals_held(case: "Case", params: dict) -> PredicateOutcome:
    """The lender holds the originals that constitute the security.

    For an equitable mortgage the originals deposited ARE the security (TPA s.58(f)), so
    a photocopy in place of an original is not a filing inconvenience — it goes to whether
    security was created at all.
    """
    required_types = [DocumentType(t) for t in params.get("document_types", [])]
    if not required_types:
        return PredicateOutcome.not_determinable("No document types configured.")

    present = {d.document_type: d for d in case.documents}
    not_original, absent = [], []
    for t in required_types:
        doc = present.get(t)
        if doc is None:
            absent.append(t.value)
        elif doc.custody.value != "original_held":
            not_original.append(f"{t.value} ({doc.custody.value})")

    if absent:
        return PredicateOutcome(
            determination=Determination.MISSING,
            evidence=Evidence(note=f"Not in bundle: {', '.join(absent)}"),
            message_vars={"detail": ", ".join(absent)},
        )
    if not_original:
        return PredicateOutcome(
            determination=Determination.MISMATCH,
            evidence=Evidence(
                document_ids=[present[t].document_id for t in required_types],
                note=f"Original not held for: {', '.join(not_original)}",
            ),
            message_vars={"detail": ", ".join(not_original)},
        )
    return PredicateOutcome(
        determination=Determination.MATCH,
        evidence=Evidence(
            document_ids=[present[t].document_id for t in required_types],
            note="Originals held for all required documents.",
        ),
        message_vars={"detail": ""},
    )


# =====================================================================================
# External verification
#
# Two shapes, because "does the owner match the land record?" and "does a prior charge
# exist?" are different questions. Collapsing them into one predicate would force one of
# them to be expressed backwards.
# =====================================================================================

#: Worst-first, so a single contradiction is not hidden by other results agreeing.
_STATUS_RANK: dict[VerificationStatus, int] = {
    VerificationStatus.MISMATCH: 0,
    VerificationStatus.NOT_FOUND_IN_SOURCE: 1,
    VerificationStatus.PARTIAL_MATCH: 2,
    VerificationStatus.STALE: 3,
    VerificationStatus.MATCH: 4,
    VerificationStatus.NOT_APPLICABLE: 5,
    VerificationStatus.SOURCE_UNAVAILABLE: 6,
    VerificationStatus.PENDING_MANUAL: 7,
}


def _evidence_from(results: list[VerificationResult]) -> Evidence:
    return Evidence(
        claim_ids=[cid for r in results for cid in r.internal_claim_ids],
        external_snapshot_ids=[r.snapshot_id for r in results if r.snapshot_id],
        note="; ".join(f"{r.source_id}/{r.attribute}: {r.status.value}" for r in results),
    )


def _unusable(results: list[VerificationResult], what: str) -> PredicateOutcome | None:
    """Return NOT_DETERMINABLE when nothing usable came back.

    Deliberately covers unavailable sources, pending operator tasks and any result whose
    access tier cannot support a conclusion. None of these is a failure - the check simply
    did not happen, and saying otherwise would let an unreachable portal look like an
    adverse finding.
    """
    if not results:
        return PredicateOutcome.not_determinable(
            f"No external verification was attempted for {what}."
        )

    scoped_out = [r for r in results
                  if r.attribute == "*" and r.status is VerificationStatus.NOT_APPLICABLE]
    if scoped_out and not any(r.counts_as_a_check for r in results):
        return PredicateOutcome(
            determination=Determination.NOT_APPLICABLE,
            evidence=_evidence_from(results),
            message_vars={"reason": scoped_out[0].detail},
        )

    answered = [r for r in results if r.counts_as_a_check]
    if not answered:
        pending = sum(1 for r in results
                      if r.status is VerificationStatus.PENDING_MANUAL)
        unavailable = sum(1 for r in results
                          if r.status is VerificationStatus.SOURCE_UNAVAILABLE)
        return PredicateOutcome.not_determinable(
            f"No authority answered for {what} "
            f"({pending} pending operator task(s), {unavailable} unavailable). "
            f"This is an open item for case completeness, not a failure."
        )

    if all(r.tier.confidence_ceiling is ConfidenceTier.INSUFFICIENT for r in answered):
        return PredicateOutcome.not_determinable(
            f"Every answer for {what} came from a source whose access tier cannot "
            f"support a conclusion."
        )
    return None


@predicate("external_agreement")
def external_agreement(case: "Case", params: dict) -> PredicateOutcome:
    """An authoritative source agrees with what the documents say.

    Used for owner, area and parcel checks against a land record or registration entry.
    """
    attribute: str = params["attribute"]
    source_id: str | None = params.get("source_id")
    what = f"{attribute}" + (f" via {source_id}" if source_id else "")

    results = case.verification_for(source_id=source_id, attribute=attribute)
    early = _unusable(results, what)
    if early is not None:
        return early

    answered = sorted(
        (r for r in results if r.counts_as_a_check),
        key=lambda r: _STATUS_RANK[r.status],
    )
    worst = answered[0]
    evidence = _evidence_from(answered)
    vars_ = {
        "attribute": attribute,
        "source": worst.source_id,
        "authority": worst.authority,
        "internal": worst.internal_value or "(none)",
        "external": worst.external_value or "(none)",
        "detail": worst.detail,
    }

    mapping = {
        VerificationStatus.MATCH: Determination.MATCH,
        VerificationStatus.PARTIAL_MATCH: Determination.PARTIAL_MATCH,
        VerificationStatus.MISMATCH: Determination.MISMATCH,
        VerificationStatus.NOT_FOUND_IN_SOURCE: Determination.MISSING,
        # A stale record is not evidence of agreement, but it is not a contradiction
        # either - the underlying record may simply not have been updated.
        VerificationStatus.STALE: Determination.NOT_DETERMINABLE,
    }
    return PredicateOutcome(
        determination=mapping.get(worst.status, Determination.NOT_DETERMINABLE),
        evidence=evidence,
        message_vars=vars_,
    )


@predicate("external_record_presence")
def external_record_presence(case: "Case", params: dict) -> PredicateOutcome:
    """Whether an authoritative register holds a record at all.

    This is a PRESENCE question, not a comparison. For CERSAI the meaningful answer is
    "is there a subsisting charge?", and there is normally nothing in the borrower's own
    documents to compare against - so a comparison predicate would report NOT_APPLICABLE
    and lose the very signal we came for.

    `presence_means` inverts the reading:
      * `adverse`   - a record existing is bad (a prior charge on the collateral)
      * `expected`  - a record is expected to exist (the property should be on the register)
    """
    attribute: str = params["attribute"]
    source_id: str | None = params.get("source_id")
    presence_means: str = params.get("presence_means", "adverse")
    what = f"{attribute}" + (f" via {source_id}" if source_id else "")

    results = case.verification_for(source_id=source_id, attribute=attribute)
    if not results:
        return PredicateOutcome.not_determinable(
            f"No external verification was attempted for {what}."
        )

    # Presence is judged directly from the results, NOT through the generic "was this a
    # usable check" gate. A record-bearing result may carry any comparison status,
    # including NOT_APPLICABLE where we had nothing internal to compare against - and for
    # a prior-charge check that IS the answer.
    with_record = [r for r in results if r.external_value]
    not_found = [r for r in results
                 if r.status is VerificationStatus.NOT_FOUND_IN_SOURCE]

    present = bool(with_record)
    if not present and not not_found:
        scoped_out = [r for r in results if r.attribute == "*"
                      and r.status is VerificationStatus.NOT_APPLICABLE]
        if scoped_out:
            return PredicateOutcome.not_applicable(scoped_out[0].detail)
        pending = sum(1 for r in results
                      if r.status is VerificationStatus.PENDING_MANUAL)
        unavailable = sum(1 for r in results
                          if r.status is VerificationStatus.SOURCE_UNAVAILABLE)
        return PredicateOutcome.not_determinable(
            f"No authority reported presence or absence for {what} "
            f"({pending} pending operator task(s), {unavailable} unavailable). "
            f"This is an open item for case completeness, not a failure."
        )

    relevant = with_record or not_found
    evidence = _evidence_from(relevant)
    vars_ = {
        "attribute": attribute,
        "source": relevant[0].source_id,
        "authority": relevant[0].authority,
        "external": relevant[0].external_value or "(no record)",
        "detail": relevant[0].detail,
    }

    if presence_means == "expected":
        determination = Determination.MATCH if present else Determination.MISSING
    else:
        determination = Determination.MISMATCH if present else Determination.MATCH
    return PredicateOutcome(determination=determination, evidence=evidence,
                            message_vars=vars_)


@predicate("verification_coverage")
def verification_coverage(case: "Case", params: dict) -> PredicateOutcome:
    """How much of the planned external verification actually happened.

    Reports case COMPLETENESS, which is deliberately separate from pass/fail. An
    unreachable portal or an outstanding operator task is an open item, and burying either
    in a pass rate would overstate how much the system has established.
    """
    results = case.verification_results
    if not results:
        return PredicateOutcome.not_determinable(
            "No external verification was attempted for this case."
        )

    answered = [r for r in results if r.counts_as_a_check]
    pending = [r for r in results if r.status is VerificationStatus.PENDING_MANUAL]
    unavailable = [r for r in results
                   if r.status is VerificationStatus.SOURCE_UNAVAILABLE]
    in_scope = [r for r in results if r.status is not VerificationStatus.NOT_APPLICABLE]

    vars_ = {
        "answered": len(answered),
        "in_scope": len(in_scope),
        "pending": len(pending),
        "unavailable": len(unavailable),
    }
    note = (
        f"{len(answered)} of {len(in_scope)} in-scope external checks answered; "
        f"{len(pending)} awaiting an operator, {len(unavailable)} unavailable."
    )

    if not in_scope:
        return PredicateOutcome(
            determination=Determination.NOT_APPLICABLE,
            evidence=Evidence(note="No external source was in scope for this case."),
            message_vars=vars_,
        )
    if not answered:
        return PredicateOutcome(
            determination=Determination.NOT_DETERMINABLE,
            evidence=Evidence(note=note),
            message_vars={**vars_, "reason": note},
        )
    determination = (
        Determination.MATCH if len(answered) == len(in_scope)
        else Determination.PARTIAL_MATCH
    )
    return PredicateOutcome(determination=determination,
                            evidence=Evidence(note=note), message_vars=vars_)


# =====================================================================================
# LTV - REQ_HFC_19_1_LTV_CAP and REQ_HFC_AXIV_1_9
# =====================================================================================


@predicate("ltv_within_cap")
def ltv_within_cap(case: "Case", params: dict) -> PredicateOutcome:
    """LTV is within the slab cap for the loan amount.

    Slabs come from the rule's params so the thresholds stay visible in YAML. Boundaries
    are INCLUSIVE at the top of each slab, matching "up to Rs.30 lakh" and "above Rs.30
    lakh and up to Rs.75 lakh". A half-open interval would misclassify a loan of exactly
    Rs.30,00,000.
    """
    loan = case.loan
    ltv = loan.ltv_percent()
    numerator = loan.ltv_numerator()

    if ltv is None or numerator is None:
        missing = []
        if numerator is None:
            missing.append("loan outstanding/sanctioned amount")
        if loan.property_value_for_ltv is None:
            missing.append("property value for LTV")
        return PredicateOutcome.not_determinable(
            f"Cannot compute LTV; missing {' and '.join(missing)}."
        )

    slabs = params.get("slabs", [])
    if not slabs:
        return PredicateOutcome.not_determinable("No LTV slabs configured.")

    amount_paise = numerator.paise
    cap: Decimal | None = None
    slab_label = ""
    for slab in slabs:
        max_loan = slab.get("max_loan_rupees")
        if max_loan is None or amount_paise <= int(Decimal(str(max_loan)) * 100):
            cap = Decimal(str(slab["max_ltv_pct"]))
            slab_label = slab.get("label", "")
            break

    if cap is None:
        return PredicateOutcome.not_determinable("Loan amount matched no configured slab.")

    vars_ = {
        "ltv": f"{ltv}",
        "cap": f"{cap}",
        "slab": slab_label,
        "amount": str(numerator),
    }
    if ltv <= cap:
        return PredicateOutcome(
            determination=Determination.MATCH,
            evidence=Evidence(note=f"LTV {ltv}% within cap {cap}% for {slab_label}."),
            message_vars=vars_,
        )
    return PredicateOutcome(
        determination=Determination.MISMATCH,
        evidence=Evidence(note=f"LTV {ltv}% exceeds cap {cap}% for {slab_label}."),
        message_vars=vars_,
    )


@predicate("ltv_value_not_above_documented_consideration")
def ltv_value_not_above_documented_consideration(
    case: "Case", params: dict
) -> PredicateOutcome:
    """REQ_HFC_AXIV_1_9.

    The property value used for LTV must not exceed the documented transaction value in
    the agreement to sale / sale deed. Applies only to an initial purchase.

    This is the highest-value checkpoint in the MVP: it names two of the five MVP document
    types, needs no external source, and turns an extracted consideration amount directly
    into a regulatory check. It also gives cross-document consistency a regulatory purpose
    — if the documents disagree on consideration, the checkpoint is NOT_DETERMINABLE
    rather than merely untidy.
    """
    from ..model.case import TransactionType

    attribute = params.get("attribute", "transaction.consideration")

    if case.loan.transaction_type is TransactionType.UNKNOWN:
        return PredicateOutcome.not_determinable(
            "Transaction type not established; Annex XIV 1.9 applies to an initial "
            "purchase only."
        )
    if case.loan.transaction_type is TransactionType.LOAN_AGAINST_OWNED_PROPERTY:
        return PredicateOutcome.not_applicable(
            "Not an initial purchase transaction; Annex XIV 1.9 does not arise."
        )

    value = case.loan.property_value_for_ltv
    if value is None:
        return PredicateOutcome.not_determinable("Property value for LTV not recorded.")
    if not case.properties:
        return PredicateOutcome.not_determinable("No property on the case.")

    res = case.properties[0].resolve(attribute)
    if res.determination is Determination.MISSING:
        return PredicateOutcome.not_determinable(
            "No documented consideration extracted from the agreement to sale or sale deed."
        )
    if res.determination is Determination.MISMATCH:
        return PredicateOutcome(
            determination=Determination.NOT_DETERMINABLE,
            evidence=Evidence(
                claim_ids=[*res.supporting_claim_ids, *res.conflicting_claim_ids],
                note=(
                    "Documents disagree on the consideration amount, so the Annex XIV 1.9 "
                    "cap cannot be applied until the conflict is resolved."
                ),
            ),
            message_vars={"reason": "conflicting consideration amounts across documents"},
        )

    documented = res.value.amount  # MoneyValue
    vars_ = {"value": str(value), "documented": str(documented)}
    if value.paise <= documented.paise:
        return PredicateOutcome(
            determination=Determination.MATCH,
            evidence=Evidence(
                claim_ids=res.supporting_claim_ids,
                note=f"LTV property value {value} does not exceed documented {documented}.",
            ),
            message_vars=vars_,
        )
    return PredicateOutcome(
        determination=Determination.MISMATCH,
        evidence=Evidence(
            claim_ids=res.supporting_claim_ids,
            note=f"LTV property value {value} exceeds documented {documented}.",
        ),
        message_vars=vars_,
    )
