"""Comparing extracted values against ground truth.

Reuses the platform's own comparison semantics — area tolerance and measurement basis,
typed parcel keys, scored name matching — so the harness judges a value the same way the
system does. A harness with its own stricter or looser notion of equality would measure
something other than the product.

`NEAR` exists for the band where a value is plausible but not acceptable: a name that
scores in the review band, or an area on an unstated basis. Scoring those as `CORRECT`
would flatter the system; scoring them as `WRONG` would overstate the danger, since both
route to a human rather than through as an answer.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from ..extract.normalize import parse_date
from ..model.claims import (
    AreaValue,
    BoolValue,
    ClaimValue,
    DateValue,
    MoneyValue,
    ParcelValue,
    TextValue,
)
from ..model.common import Area, AreaUnit, Determination
from ..resolve.names import match_names
from .metrics import Outcome

#: Fields compared as people's names rather than as plain text.
NAME_FIELDS = {
    "seller", "buyer", "owner", "assessee", "mortgagor", "mortgagee",
    "promoter", "recipient",
}

#: Area agreement tolerance, matching the cross-document default.
AREA_TOLERANCE_PCT = Decimal("2")


def _as_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError, TypeError):
        return None


def _expected_area(expected) -> Area | None:
    """Ground truth may state an area as a mapping or as a bare number of sq ft."""
    if isinstance(expected, dict):
        value = _as_decimal(expected.get("value"))
        unit = expected.get("unit", "sq_ft")
        if value is None:
            return None
        try:
            return Area.of(value, AreaUnit(unit))
        except ValueError:
            return None
    value = _as_decimal(expected)
    return Area.of(value, AreaUnit.SQ_FT) if value is not None else None


def compare_to_truth(
    field_name: str, expected, actual: ClaimValue
) -> tuple[Outcome, str]:
    """Compare one extracted value against its reference."""
    if isinstance(actual, MoneyValue):
        want = _as_decimal(expected)
        if want is None:
            return Outcome.NOT_EVALUATED, f"unparseable reference {expected!r}"
        got = actual.amount.rupees
        if got == want:
            return Outcome.CORRECT, ""
        return Outcome.WRONG, f"expected {want}, got {got}"

    if isinstance(actual, DateValue):
        want = expected if isinstance(expected, date) else None
        if want is None:
            parsed = parse_date(str(expected))
            # An ISO reference is unambiguous; parse it directly rather than through the
            # day-first document parser, which would read 2024-03-14 as a numeric date.
            try:
                want = date.fromisoformat(str(expected).strip())
            except ValueError:
                want = parsed.value if parsed else None
        if want is None:
            return Outcome.NOT_EVALUATED, f"unparseable reference {expected!r}"
        if actual.value == want:
            return Outcome.CORRECT, ""
        return Outcome.WRONG, f"expected {want}, got {actual.value}"

    if isinstance(actual, ParcelValue):
        want = str(expected).strip().upper().replace(" ", "")
        got = actual.identifier.value.upper().replace(" ", "")
        if got == want:
            return Outcome.CORRECT, ""
        return Outcome.WRONG, f"expected {want}, got {got}"

    if isinstance(actual, AreaValue):
        want_area = _expected_area(expected)
        if want_area is None:
            return Outcome.NOT_EVALUATED, f"unparseable reference {expected!r}"
        want_basis = expected.get("basis") if isinstance(expected, dict) else None
        if not actual.area.matches(want_area, AREA_TOLERANCE_PCT):
            return Outcome.WRONG, f"expected {want_area}, got {actual.area}"
        if want_basis and actual.basis != want_basis:
            # Right magnitude, wrong basis. Comparable areas on different bases are not
            # interchangeable, so this is a review case rather than a clean pass.
            return Outcome.NEAR, f"basis {actual.basis} != {want_basis}"
        return Outcome.CORRECT, ""

    if isinstance(actual, BoolValue):
        want = str(expected).strip().lower() in ("true", "yes", "1")
        return (Outcome.CORRECT, "") if actual.value == want else (
            Outcome.WRONG, f"expected {want}, got {actual.value}")

    if isinstance(actual, TextValue):
        want = str(expected).strip()
        if field_name in NAME_FIELDS:
            m = match_names(want, actual.raw)
            if m.determination is Determination.MATCH:
                return Outcome.CORRECT, ""
            if m.determination is Determination.PARTIAL_MATCH:
                return Outcome.NEAR, f"name score {m.score:.2f}"
            return Outcome.WRONG, f"expected {want!r}, got {actual.raw!r}"
        if actual.comparable() == want.casefold():
            return Outcome.CORRECT, ""
        # Identifiers often differ only in punctuation or spacing.
        squash = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())  # noqa: E731
        if squash(actual.raw) == squash(want):
            return Outcome.NEAR, "differs only in punctuation or spacing"
        return Outcome.WRONG, f"expected {want!r}, got {actual.raw!r}"

    return Outcome.NOT_EVALUATED, f"unsupported value type {type(actual).__name__}"


def best_outcome(
    field_name: str, expected, candidates: list[ClaimValue]
) -> tuple[Outcome, str]:
    """Best outcome across several extracted values for one field.

    A field may legitimately yield several claims — the model preserves competing values
    rather than resolving them. For measurement, the system is credited if any of them is
    right, and the detail records how many were offered so a rule producing five candidates
    to hit one is not silently rewarded.
    """
    if not candidates:
        return Outcome.MISSING, "not extracted"

    rank = {
        Outcome.CORRECT: 0, Outcome.NEAR: 1, Outcome.WRONG: 2,
        Outcome.NOT_EVALUATED: 3, Outcome.MISSING: 4, Outcome.SPURIOUS: 5,
    }
    scored = [compare_to_truth(field_name, expected, c) for c in candidates]
    scored.sort(key=lambda pair: rank[pair[0]])
    outcome, detail = scored[0]
    if len(candidates) > 1:
        detail = f"{detail} ({len(candidates)} candidates offered)".strip()
    return outcome, detail
