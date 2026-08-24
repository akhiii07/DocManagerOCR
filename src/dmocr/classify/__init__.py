"""Document classification: which extraction schema applies."""

from .classifier import (
    ClassificationResult,
    ClassifierConfig,
    RuleClassifier,
    SignalHit,
    UnknownReason,
    apply_to_document,
)
from .signals import Signal, all_signals, known_types, signals_for

__all__ = [
    "ClassificationResult",
    "ClassifierConfig",
    "RuleClassifier",
    "Signal",
    "SignalHit",
    "UnknownReason",
    "all_signals",
    "apply_to_document",
    "known_types",
    "signals_for",
]
