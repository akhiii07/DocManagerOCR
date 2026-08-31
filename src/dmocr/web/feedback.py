"""Reviewer feedback on extracted fields.

The "data confirmation" step: a reviewer accepts a value or corrects it. This is more than
a UI nicety — it is the only source of ground truth the system will ever generate from
real use, and therefore the input to confidence calibration (OPEN-ITEMS 41).

Two design points:

**A correction is a new claim, not an edit.** Claims are immutable (ADR-0003). A correction
becomes a claim with `HumanProvenance`, and the original stays in the feedback log. An
overridden value must remain auditable, because "why did the system say X?" is a question
that gets asked months later.

**A human claim is legitimately ungrounded.** ADR-0004 requires that a value the *model*
produces be locatable on the page. A reviewer asserting a value is a different kind of
claim entirely — they are the authority, not the page. The provenance kind records which
it was, so the distinction survives into the audit.

What makes the log useful for calibration is that it records the **original confidence**
alongside the outcome. "The system said HIGH and the reviewer corrected it" is the signal;
the corrected value alone is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from ..extract.normalize import parse_area, parse_date, parse_money_figures
from ..model.claims import (
    AreaValue,
    BoolValue,
    ClaimValue,
    DateValue,
    MoneyValue,
    ParcelValue,
    TextValue,
)
from ..model.common import Money, ParcelIdentifier

log = logging.getLogger(__name__)


class FeedbackAction(StrEnum):
    ACCEPTED = "accepted"
    CORRECTED = "corrected"


@dataclass
class FieldFeedback:
    """One reviewer decision about one extracted field."""

    document_id: str
    field_name: str
    action: FeedbackAction
    #: What the system produced, and how sure it said it was. Both are needed for
    #: calibration - the outcome alone says nothing about whether confidence was earned.
    original_value: str
    original_confidence: str
    corrected_value: str | None = None
    actor: str = "local-operator"
    at: datetime = field(default_factory=datetime.now)

    @property
    def key(self) -> tuple[str, str]:
        return (self.document_id, self.field_name)

    @property
    def contradicted_the_system(self) -> bool:
        return self.action is FeedbackAction.CORRECTED


class FeedbackLog:
    """Reviewer decisions for the session.

    In-memory, like the rest of the session. A production deployment persists this — it is
    the calibration dataset, and it is also the only record of who overrode what.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], FieldFeedback] = {}
        self._history: list[FieldFeedback] = []

    def record(self, entry: FieldFeedback) -> None:
        self._entries[entry.key] = entry
        # Append-only history: a reviewer changing their mind must not erase the fact
        # that they decided something different first.
        self._history.append(entry)

    def get(self, document_id: str, field_name: str) -> FieldFeedback | None:
        return self._entries.get((document_id, field_name))

    def for_document(self, document_id: str) -> list[FieldFeedback]:
        return [e for e in self._entries.values() if e.document_id == document_id]

    def corrections(self) -> list[FieldFeedback]:
        return [e for e in self._entries.values() if e.contradicted_the_system]

    @property
    def history(self) -> list[FieldFeedback]:
        return list(self._history)

    def clear(self) -> None:
        self._entries.clear()
        self._history.clear()

    def calibration_summary(self) -> dict:
        """Correction rate by the confidence the system claimed.

        The headline calibration question: when the system said HIGH, how often was it
        wrong? A high correction rate on HIGH-confidence fields means the confidence
        signal is not earning its name.
        """
        buckets: dict[str, dict[str, int]] = {}
        for e in self._entries.values():
            b = buckets.setdefault(e.original_confidence, {"accepted": 0, "corrected": 0})
            b[e.action.value] += 1
        for stats in buckets.values():
            total = stats["accepted"] + stats["corrected"]
            stats["correction_rate"] = (
                round(stats["corrected"] / total, 4) if total else None
            )
        return buckets

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return True


class CorrectionError(ValueError):
    """The reviewer's text could not be read as a value of the expected kind."""


def parse_correction(original: ClaimValue, text: str) -> ClaimValue:
    """Turn a reviewer's typed correction into a value of the same kind as the original.

    Refuses rather than guesses. Silently coercing "eleven lakh" to zero, or a malformed
    date to today, would put a wrong value into the case under a human's authority - which
    is worse than the extraction error being corrected.
    """
    raw = (text or "").strip()
    if not raw:
        raise CorrectionError("Enter a value.")

    if isinstance(original, MoneyValue):
        parsed = parse_money_figures(raw)
        if parsed is not None:
            return MoneyValue(amount=parsed.amount)
        try:
            return MoneyValue(amount=Money.from_rupees(
                Decimal(raw.replace(",", "").replace("₹", "").strip())))
        except (InvalidOperation, ValueError):
            raise CorrectionError(
                "Could not read that as an amount. Try 1250000 or Rs. 12,50,000.") from None

    if isinstance(original, DateValue):
        from datetime import date as _date

        try:
            return DateValue(value=_date.fromisoformat(raw))
        except ValueError:
            pass
        parsed = parse_date(raw)
        if parsed is None:
            raise CorrectionError(
                "Could not read that as a date. Try 2024-03-14 or 14/03/2024.")
        return DateValue(value=parsed.value)

    if isinstance(original, AreaValue):
        parsed = parse_area(raw)
        if parsed is None:
            raise CorrectionError(
                "Could not read that as an area. Include the unit, e.g. 1150 sq ft.")
        # Keep the original measurement basis unless the correction restates one -
        # otherwise correcting a number would silently drop "carpet" and make the value
        # incomparable with the other documents.
        basis = parsed.basis if parsed.basis != "unspecified" else original.basis
        return AreaValue(area=parsed.area, basis=basis)  # type: ignore[arg-type]

    if isinstance(original, ParcelValue):
        return ParcelValue(identifier=ParcelIdentifier(
            id_type=original.identifier.id_type,
            value=raw,
            locality=original.identifier.locality,
        ))

    if isinstance(original, BoolValue):
        low = raw.lower()
        if low in ("yes", "true", "y", "1"):
            return BoolValue(value=True)
        if low in ("no", "false", "n", "0"):
            return BoolValue(value=False)
        raise CorrectionError("Enter yes or no.")

    if isinstance(original, TextValue):
        return TextValue(raw=raw, normalised=raw.upper())

    raise CorrectionError("This field cannot be corrected here.")
