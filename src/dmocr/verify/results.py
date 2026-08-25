"""External verification results.

The status vocabulary is wider than MATCH/MISMATCH on purpose, because the ways an
external check can fail to produce an answer are materially different from each other and
a Risk Manager needs to tell them apart.

**The rule that matters most: `SOURCE_UNAVAILABLE` is not a compliance failure.** A portal
being down says nothing about the collateral. Conflating "we could not check" with "the
check failed" would make the system untrustworthy in the first direction reviewers notice,
and it is the single easiest mistake to make in this layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..model.claims import ClaimValue
from ..model.common import ConfidenceTier, Severity


class AccessTier(StrEnum):
    """How a source can lawfully be reached (ADR-0005).

    The tier caps the confidence of anything the source tells us, and determines whether
    an adapter can run unattended or needs a human.
    """

    T1_OFFICIAL_API = "T1"          # documented API under an agreement
    T2_LICENSED = "T2"              # regulated intermediary with a lawful basis
    T3_PORTAL_PERMITTED = "T3"      # public portal whose terms permit automation
    T4_PORTAL_MANUAL = "T4"         # portal, human-operated (CAPTCHA / terms forbid)
    T5_OFFLINE = "T5"               # physical or application-based retrieval
    T6_UNAVAILABLE = "T6"           # no source for this jurisdiction/field

    @property
    def is_automatable(self) -> bool:
        return self in (
            AccessTier.T1_OFFICIAL_API,
            AccessTier.T2_LICENSED,
            AccessTier.T3_PORTAL_PERMITTED,
        )

    @property
    def needs_human(self) -> bool:
        return self in (AccessTier.T4_PORTAL_MANUAL, AccessTier.T5_OFFLINE)

    @property
    def confidence_ceiling(self) -> ConfidenceTier:
        """A statutory API is worth more than an operator's screenshot."""
        if self in (AccessTier.T1_OFFICIAL_API, AccessTier.T2_LICENSED):
            return ConfidenceTier.HIGH
        if self in (AccessTier.T3_PORTAL_PERMITTED, AccessTier.T4_PORTAL_MANUAL,
                    AccessTier.T5_OFFLINE):
            return ConfidenceTier.MEDIUM
        return ConfidenceTier.INSUFFICIENT


class VerificationStatus(StrEnum):
    MATCH = "MATCH"
    #: Agrees on some components but not all - e.g. project matches, phase does not.
    #: A distinct outcome because RERA phases are separately registered, so a name
    #: difference is often expected rather than wrong.
    PARTIAL_MATCH = "PARTIAL_MATCH"
    MISMATCH = "MISMATCH"
    #: The source responded and holds no such record. A signal, sometimes a serious one -
    #: NOT an error.
    NOT_FOUND_IN_SOURCE = "NOT_FOUND_IN_SOURCE"
    #: Source unreachable. NEVER a compliance failure.
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    #: The question does not arise for this property.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Data returned but older than the field's freshness policy.
    STALE = "STALE"
    #: Queued for a human operator (T4/T5) and not yet retrieved.
    PENDING_MANUAL = "PENDING_MANUAL"

    @property
    def is_adverse(self) -> bool:
        """Only a contradiction or a missing record counts against the case."""
        return self in (VerificationStatus.MISMATCH,
                        VerificationStatus.NOT_FOUND_IN_SOURCE)

    @property
    def is_answered(self) -> bool:
        """Whether the source actually told us something."""
        return self in (
            VerificationStatus.MATCH,
            VerificationStatus.PARTIAL_MATCH,
            VerificationStatus.MISMATCH,
            VerificationStatus.NOT_FOUND_IN_SOURCE,
            VerificationStatus.STALE,
        )


class Snapshot(BaseModel):
    """Immutable record of what a source returned, and when.

    External data is snapshotted and never re-fetched during a re-run. Without this a case
    cannot be reproduced, because the outside world will have moved on - and a finding
    that cannot be reproduced cannot be defended.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(default_factory=lambda: f"SNAP_{uuid.uuid4().hex[:12]}")
    source_id: str
    authority: str
    retrieved_at: datetime
    tier: AccessTier
    #: Set when a human operated the source on our behalf (T4/T5).
    operator_id: str | None = None
    url: str | None = None
    #: What was sent. Recorded for the data-minimisation audit: an external lookup is an
    #: outbound disclosure of customer data, not a neutral read.
    request_keys: dict[str, str] = Field(default_factory=dict)
    #: Opaque reference to the stored artefact (response body or page capture).
    artefact_ref: str | None = None
    notes: str | None = None


class ExternalObservation(BaseModel):
    """What an authoritative source says, normalised into claim values."""

    model_config = ConfigDict(frozen=True)

    snapshot: Snapshot
    #: attribute -> value, using the same canonical attributes as internal claims so the
    #: comparison is like-for-like.
    fields: dict[str, ClaimValue] = Field(default_factory=dict)
    #: True when the source responded but holds no matching record.
    record_found: bool = True
    #: Age of the underlying record, where the source reports it.
    record_as_of: datetime | None = None

    def is_stale(self, max_age: timedelta | None) -> bool:
        if max_age is None or self.record_as_of is None:
            return False
        return (self.snapshot.retrieved_at - self.record_as_of) > max_age


class VerificationResult(BaseModel):
    """One attribute checked against one source."""

    model_config = ConfigDict(frozen=True)

    verification_id: str = Field(default_factory=lambda: f"VER_{uuid.uuid4().hex[:10]}")
    source_id: str
    authority: str
    attribute: str
    status: VerificationStatus
    tier: AccessTier

    internal_value: str | None = None
    external_value: str | None = None
    #: Claim ids the internal value came from, so the reviewer can trace both sides.
    internal_claim_ids: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None

    confidence: ConfidenceTier = ConfidenceTier.INSUFFICIENT
    detail: str = ""
    checked_at: datetime | None = None

    @property
    def review_required(self) -> bool:
        return self.status.is_adverse or self.status is VerificationStatus.PARTIAL_MATCH

    @property
    def counts_as_a_check(self) -> bool:
        """Whether this contributes to compliance coverage at all.

        An unavailable source or a pending manual task contributes to *case completeness*,
        which is reported separately, but never to pass/fail.
        """
        return self.status.is_answered

    def severity_hint(self) -> Severity | None:
        """Suggested severity. The rule that consumes this decides the real one."""
        if self.status is VerificationStatus.MISMATCH:
            return Severity.HIGH
        if self.status is VerificationStatus.NOT_FOUND_IN_SOURCE:
            return Severity.MEDIUM
        if self.status is VerificationStatus.PARTIAL_MATCH:
            return Severity.MEDIUM
        return None
