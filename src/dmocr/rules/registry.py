"""Predicate registry.

Rules name a predicate; predicates are registered Python functions. This is deliberately
*not* an expression language evaluated over arbitrary strings from a YAML file.

The trade-off was considered. A CEL/JSONLogic expression layer would let rule authors
write conditions without touching Python, which is attractive. But the conditions this
domain actually needs — comparing claim sets with tolerance and measurement basis,
filtering by instrument strength, applying a conditional registration carve-out — are not
one-line comparisons. Expressing them in an embedded language would either be unreadable
or would require so many custom helper functions that the helpers become the real
implementation anyway.

So: named predicates in Python, reviewed as code and tested as code, with all the
*policy* (severity, applicability, citations, message, sign-off) in YAML where non-
engineers can read and diff it. A simple expression layer can be added later for trivial
conditions without changing this contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..model.common import Determination
from ..model.findings import Evidence

if TYPE_CHECKING:
    from ..model.case import Case


@dataclass(frozen=True)
class PredicateOutcome:
    """What a predicate returns.

    `message_vars` feed the rule's message template, so the human-readable text lives in
    YAML with the rest of the policy while the numbers come from the computation.
    """

    determination: Determination
    evidence: Evidence = field(default_factory=Evidence)
    message_vars: dict = field(default_factory=dict)

    @classmethod
    def not_determinable(cls, why: str, **vars) -> "PredicateOutcome":
        return cls(
            determination=Determination.NOT_DETERMINABLE,
            evidence=Evidence(note=why),
            message_vars={"reason": why, **vars},
        )

    @classmethod
    def not_applicable(cls, why: str) -> "PredicateOutcome":
        return cls(
            determination=Determination.NOT_APPLICABLE,
            evidence=Evidence(note=why),
            message_vars={"reason": why},
        )


Predicate = Callable[["Case", dict], PredicateOutcome]

_REGISTRY: dict[str, Predicate] = {}


def predicate(name: str) -> Callable[[Predicate], Predicate]:
    """Register a predicate under `name`, as referenced by a rule's `check` field."""

    def wrap(fn: Predicate) -> Predicate:
        if name in _REGISTRY:
            raise ValueError(f"predicate {name!r} already registered")
        _REGISTRY[name] = fn
        return fn

    return wrap


def get_predicate(name: str) -> Predicate:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown predicate {name!r}. Registered: {sorted(_REGISTRY)}"
        ) from None


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def clear_registry() -> None:
    """Test support only."""
    _REGISTRY.clear()
