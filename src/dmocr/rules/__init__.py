"""Rule engine: declarative specs in YAML, named predicates in Python.

Importing this package registers the built-in predicates, so a `RuleSet` loaded from YAML
can resolve its `check` names without the caller having to import them explicitly.
"""

from . import predicates as _predicates  # noqa: F401  (registers predicates on import)
from .engine import ExecutionMode, RuleEngine, summarise
from .registry import PredicateOutcome, get_predicate, predicate, registered_names
from .spec import Applicability, LegalSignoff, RuleSet, RuleSpec, RuleStatus

__all__ = [
    "Applicability",
    "ExecutionMode",
    "LegalSignoff",
    "PredicateOutcome",
    "RuleEngine",
    "RuleSet",
    "RuleSpec",
    "RuleStatus",
    "get_predicate",
    "predicate",
    "registered_names",
    "summarise",
]
