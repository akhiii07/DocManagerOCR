"""Entity resolution: deciding when two documents describe the same person or parcel."""

from .assembler import (
    PARTY_ATTRIBUTES,
    AssemblyResult,
    CaseAssembler,
    ResolutionDecision,
)
from .names import (
    MATCH_THRESHOLD,
    MISMATCH_THRESHOLD,
    NameMatch,
    NameParts,
    best_match,
    match_names,
    phonetic_key,
    similarity,
    split_name,
)

__all__ = [
    "MATCH_THRESHOLD",
    "MISMATCH_THRESHOLD",
    "PARTY_ATTRIBUTES",
    "AssemblyResult",
    "CaseAssembler",
    "NameMatch",
    "NameParts",
    "ResolutionDecision",
    "best_match",
    "match_names",
    "phonetic_key",
    "similarity",
    "split_name",
]
