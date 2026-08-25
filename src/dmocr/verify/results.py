"""Verification result model.

Re-exported from `dmocr.model.verification`. The result TYPES live in the model layer so
`Case` can hold verification results without a circular import: orchestration depends on
the model, never the other way round.

Kept as a module so existing imports from `dmocr.verify.results` continue to work.
"""

from ..model.verification import (
    AccessTier,
    ExternalObservation,
    Snapshot,
    VerificationResult,
    VerificationStatus,
)

__all__ = [
    "AccessTier",
    "ExternalObservation",
    "Snapshot",
    "VerificationResult",
    "VerificationStatus",
]
