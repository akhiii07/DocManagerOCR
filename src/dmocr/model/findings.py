"""Check results and findings.

A `CheckResult` is what a rule produced. A `Finding` is that result dressed with the
rule's metadata — severity, citations, recommended action — ready for the review package.

The important design point is **disposition**, which is derived from severity AND
determinacy together rather than from severity alone. A HIGH-severity issue established
deterministically from two documents is a different thing from a HIGH-severity issue a
model merely proposed, and presenting them identically is how reviewers stop trusting the
system.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import ConfidenceTier, Determination, Severity


class Determinacy(StrEnum):
    """How the result was established. Orthogonal to how bad it is."""

    #: Computed from extracted fields with no judgement. Reproducible.
    DETERMINISTIC = "DETERMINISTIC"
    #: Confirmed against an authoritative external source.
    EXTERNAL_VERIFIED = "EXTERNAL_VERIFIED"
    #: A passage was located in the documents; a human should confirm the reading.
    RETRIEVAL_ASSISTED = "RETRIEVAL_ASSISTED"
    #: A model proposed this. Never auto-decided.
    MODEL_PROPOSED = "MODEL_PROPOSED"
    #: Requires legal or credit judgement the system cannot supply.
    HUMAN_REQUIRED = "HUMAN_REQUIRED"

    @property
    def is_machine_certain(self) -> bool:
        return self in (Determinacy.DETERMINISTIC, Determinacy.EXTERNAL_VERIFIED)


class Disposition(StrEnum):
    """What the reviewer is being asked to do."""

    #: Established, serious, and should stop the case until resolved.
    BLOCKER = "BLOCKER"
    #: Needs a human decision.
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    #: Worth knowing, no action implied.
    INFORMATIONAL = "INFORMATIONAL"
    #: Checked and satisfied.
    CLEARED = "CLEARED"
    #: The question does not arise for this case.
    NOT_APPLICABLE = "NOT_APPLICABLE"


def derive_disposition(
    determination: Determination,
    severity: Severity,
    determinacy: Determinacy,
) -> Disposition:
    """Severity x determinacy -> disposition.

    Rules, in order:

    1. `NOT_APPLICABLE` is never a finding. The question did not arise.
    2. `NOT_DETERMINABLE` is never a BLOCKER. We did not establish anything, so we must
       not stop a case on it. It escalates to REVIEW_REQUIRED only when the underlying
       issue would be serious if true.
    3. Only an adverse determination that is **machine-certain** and **serious** blocks.
       Anything a model merely proposed goes to a human, however alarming it sounds.
    """
    if determination is Determination.NOT_APPLICABLE:
        return Disposition.NOT_APPLICABLE

    if not determination.is_adverse:
        if determination is Determination.NOT_DETERMINABLE:
            # We could not tell. That is not a pass - but it is not a failure either.
            return (
                Disposition.REVIEW_REQUIRED
                if severity in (Severity.CRITICAL, Severity.HIGH)
                else Disposition.INFORMATIONAL
            )
        return Disposition.CLEARED

    # Adverse from here.
    if determinacy.is_machine_certain and severity in (Severity.CRITICAL, Severity.HIGH):
        return Disposition.BLOCKER
    if severity is Severity.INFORMATIONAL:
        return Disposition.INFORMATIONAL
    return Disposition.REVIEW_REQUIRED


class Evidence(BaseModel):
    """What supports a result. Every finding must be able to answer 'show me'."""

    model_config = ConfigDict(frozen=True)

    claim_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    external_snapshot_ids: list[str] = Field(default_factory=list)
    #: Free-text summary of what was compared. Shown to the reviewer.
    note: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.claim_ids or self.document_ids or self.external_snapshot_ids)


class CheckResult(BaseModel):
    """Raw output of evaluating one rule against one case."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_version: str
    determination: Determination
    determinacy: Determinacy
    evidence: Evidence = Field(default_factory=Evidence)
    #: Rendered from the rule's message template.
    message: str = ""
    #: Set when the rule ran but was not approved for enforcement.
    advisory_only: bool = False


class Finding(BaseModel):
    """A check result plus the rule metadata a reviewer needs to act on it."""

    model_config = ConfigDict(frozen=True)

    finding_id: str = Field(default_factory=lambda: f"F_{uuid.uuid4().hex[:10]}")
    case_id: str
    rule_id: str
    rule_version: str

    title: str
    category: str
    severity: Severity
    determinacy: Determinacy
    determination: Determination
    disposition: Disposition

    message: str
    recommended_action: str | None = None
    evidence: Evidence = Field(default_factory=Evidence)
    confidence: ConfidenceTier = ConfidenceTier.MEDIUM

    #: Regulatory grounding. Empty means this is a BUSINESS RULE, not a regulatory
    #: checkpoint - the review package must say so rather than implying legal backing.
    citations: list[str] = Field(default_factory=list)

    advisory_only: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now())

    @property
    def is_regulatory(self) -> bool:
        return bool(self.citations)

    @property
    def needs_attention(self) -> bool:
        return self.disposition in (Disposition.BLOCKER, Disposition.REVIEW_REQUIRED)


#: Ordering for the review package: what needs attention, worst first.
_DISPOSITION_ORDER = {
    Disposition.BLOCKER: 0,
    Disposition.REVIEW_REQUIRED: 1,
    Disposition.INFORMATIONAL: 2,
    Disposition.CLEARED: 3,
    Disposition.NOT_APPLICABLE: 4,
}
_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFORMATIONAL: 4,
}


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Reviewer-first ordering: 'what needs attention?', not 'what did the AI extract?'."""
    return sorted(
        findings,
        key=lambda f: (
            _DISPOSITION_ORDER[f.disposition],
            _SEVERITY_ORDER[f.severity],
            f.rule_id,
        ),
    )
