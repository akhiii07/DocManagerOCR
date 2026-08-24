"""Normalising values as written in Indian legal documents.

Three things here are genuinely error-prone and worth the care:

**Indian digit grouping.** Amounts are written 1,25,00,000 (two-two-three), not
12,500,000. Stripping commas gets the right number either way, but the *grouping* is a
useful OCR sanity signal: a figure that matches neither convention is likely misread.

**Amounts in words.** Deeds state consideration twice — "Rs. 1,25,00,000/- (Rupees One
Crore Twenty Five Lakh only)". Parsing both and comparing them is a real integrity check.
A mismatch between figures and words is a serious finding, not a formatting quirk.

**Date order.** DD/MM/YYYY is the Indian convention and is the default here. Where a date
could be read either way (both components <= 12) that is recorded on the result rather than
silently resolved, so a reviewer can be told the reading was assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ..model.common import Area, AreaUnit, Money

# =====================================================================================
# Amounts
# =====================================================================================

#: Indian grouping: 12,34,567 / 1,25,00,000. Last group of 3, earlier groups of 2.
_INDIAN_GROUPED = re.compile(r"^\d{1,2}(?:,\d{2})*,\d{3}$")
#: Western grouping: 12,345,678.
_WESTERN_GROUPED = re.compile(r"^\d{1,3}(?:,\d{3})*$")

_CURRENCY_PREFIX = r"(?:Rs\.?|INR|₹|Rupees)"

MONEY_PATTERN = re.compile(
    rf"{_CURRENCY_PREFIX}\s*([\d,]+(?:\.\d{{1,2}})?)\s*/?-?",
    re.IGNORECASE,
)

_UNITS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fourty": 40,  # common misspelling in drafted documents
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

#: Indian scale words. Order matters: larger multipliers are applied first.
_SCALES: list[tuple[tuple[str, ...], int]] = [
    (("crore", "crores", "cr"), 10_000_000),
    (("lakh", "lakhs", "lac", "lacs", "lakhs"), 100_000),
    (("thousand", "thousands"), 1_000),
    (("hundred", "hundreds"), 100),
]


class GroupingStyle:
    INDIAN = "indian"
    WESTERN = "western"
    UNGROUPED = "ungrouped"
    IRREGULAR = "irregular"


@dataclass(frozen=True)
class ParsedMoney:
    amount: Money
    raw: str
    grouping: str
    #: True when the digit grouping matches neither convention, which usually means an
    #: OCR error in the separators rather than an unusual amount.
    suspicious_grouping: bool = False


def detect_grouping(digits: str) -> str:
    if "," not in digits:
        return GroupingStyle.UNGROUPED
    if _INDIAN_GROUPED.match(digits):
        return GroupingStyle.INDIAN
    if _WESTERN_GROUPED.match(digits):
        return GroupingStyle.WESTERN
    return GroupingStyle.IRREGULAR


def parse_money_figures(text: str) -> ParsedMoney | None:
    """Parse an amount written in figures, e.g. 'Rs. 1,25,00,000/-'."""
    m = MONEY_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(1)
    integer_part = raw.split(".")[0]
    grouping = detect_grouping(integer_part)
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if value < 0:
        return None
    return ParsedMoney(
        amount=Money.from_rupees(value),
        raw=m.group(0).strip(),
        grouping=grouping,
        suspicious_grouping=grouping == GroupingStyle.IRREGULAR,
    )


def parse_indian_words(text: str) -> int | None:
    """Parse an amount in words using Indian scale terms.

    Handles 'One Crore Twenty Five Lakh Fifty Thousand only'. Returns rupees as an int,
    or None if nothing parseable was found.
    """
    cleaned = re.sub(r"[^a-z\s]", " ", text.lower())
    cleaned = re.sub(r"\b(rupees|only|and|paise)\b", " ", cleaned)
    words = [w for w in cleaned.split() if w]
    if not words:
        return None

    total = 0
    current = 0
    seen_any = False

    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
            seen_any = True
            continue
        scale = next((mult for names, mult in _SCALES if word in names), None)
        if scale is None:
            continue
        seen_any = True
        if scale >= 1_000:
            # crore / lakh / thousand close off the current group
            total += (current or 1) * scale
            current = 0
        else:
            # 'hundred' multiplies what precedes it
            current = (current or 1) * scale

    if not seen_any:
        return None
    return total + current


@dataclass(frozen=True)
class MoneyCrossCheck:
    """Comparison of an amount written in figures against the same amount in words."""

    figures: Money | None
    words_rupees: int | None
    agree: bool | None  # None = could not compare

    @property
    def is_conflict(self) -> bool:
        return self.agree is False


def cross_check_money(text: str) -> MoneyCrossCheck:
    """Compare figures against words in the same passage.

    A disagreement is a serious integrity signal - it can indicate tampering or a
    significant OCR error on a consideration amount, which feeds the LTV checks.
    """
    figures = parse_money_figures(text)
    words = None

    # Words are conventionally parenthesised after the figure.
    for candidate in re.findall(r"\(([^)]*)\)", text):
        if re.search(r"\b(crore|lakh|lac|thousand|hundred)\b", candidate, re.IGNORECASE):
            words = parse_indian_words(candidate)
            if words:
                break
    if words is None and re.search(r"\b(crore|lakh|lac)\b", text, re.IGNORECASE):
        words = parse_indian_words(text)

    if figures is None or words is None:
        return MoneyCrossCheck(figures.amount if figures else None, words, None)

    return MoneyCrossCheck(
        figures=figures.amount,
        words_rupees=words,
        agree=figures.amount.rupees == Decimal(words),
    )


# =====================================================================================
# Dates
# =====================================================================================

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

#: Trailing `(?!\d)` rather than `\b`. Where OCR has glued the next word on
#: ("March2024BETWEEN"), there is no word boundary between the year and the letter, and
#: `\b` would reject an otherwise perfectly readable date.
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})(?!\d)")
#: Separators are `\s*` not `\s+` because OCR drops word boundaries: a real recognised
#: line came back as "March2024". Requiring whitespace loses the year entirely.
_TEXTUAL_DATE = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:day\s*of\s*)?"
    r"([A-Za-z]{3,9}?)[,\s]*(\d{4})(?!\d)"
)


class DateOrder:
    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"


@dataclass(frozen=True)
class ParsedDate:
    value: date
    raw: str
    #: True when both components are <= 12, so the reading depended on the assumed order.
    ambiguous: bool = False
    order_assumed: str = DateOrder.DAY_FIRST


def parse_date(text: str, *, order: str = DateOrder.DAY_FIRST) -> ParsedDate | None:
    """Parse the first date in `text`.

    Defaults to day-first, the Indian convention. Where a numeric date could be read
    either way the result is marked `ambiguous` rather than silently resolved.
    """
    m = _TEXTUAL_DATE.search(text)
    if m:
        day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
        month = _MONTHS.get(month_name)
        if month:
            try:
                return ParsedDate(
                    value=date(int(year), month, int(day)),
                    raw=m.group(0).strip(),
                    ambiguous=False,
                    order_assumed=order,
                )
            except ValueError:
                return None

    m = _NUMERIC_DATE.search(text)
    if not m:
        return None

    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = _expand_year(y)
    day, month = (a, b) if order == DateOrder.DAY_FIRST else (b, a)
    try:
        value = date(year, month, day)
    except ValueError:
        # Try the other order before giving up: 03/14/2024 can only be month-first.
        try:
            value = date(year, day, month)
        except ValueError:
            return None
        return ParsedDate(value, m.group(0), ambiguous=False,
                          order_assumed=(DateOrder.MONTH_FIRST
                                         if order == DateOrder.DAY_FIRST
                                         else DateOrder.DAY_FIRST))

    return ParsedDate(
        value=value,
        raw=m.group(0),
        ambiguous=a <= 12 and b <= 12 and a != b,
        order_assumed=order,
    )


def _expand_year(y: int) -> int:
    """Expand a two-digit year.

    Property documents are frequently decades old, so a two-digit year is genuinely
    ambiguous. The pivot assumes anything above the current short year is last century.
    """
    if y >= 100:
        return y
    return 2000 + y if y <= 30 else 1900 + y


# =====================================================================================
# Areas
# =====================================================================================

_AREA_UNITS: list[tuple[str, AreaUnit]] = [
    (r"sq\.?\s*(?:ft|feet|foot)\.?", AreaUnit.SQ_FT),
    (r"square\s+(?:ft|feet|foot)", AreaUnit.SQ_FT),
    (r"sq\.?\s*(?:mtrs?|meters?|metres?|m)\.?", AreaUnit.SQ_M),
    (r"square\s+(?:meters?|metres?)", AreaUnit.SQ_M),
    (r"sq\.?\s*(?:yds?|yards?)\.?", AreaUnit.SQ_YARD),
    (r"gunthas?", AreaUnit.GUNTHA),
    (r"acres?", AreaUnit.ACRE),
    (r"hectares?|ha\b", AreaUnit.HECTARE),
]

AREA_PATTERN = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(" + "|".join(p for p, _ in _AREA_UNITS) + ")",
    re.IGNORECASE,
)

_AREA_BASIS = [
    (r"\bcarpet\b", "carpet"),
    (r"\bbuilt[\s\-]?up\b", "built_up"),
    (r"\bsuper\s+built[\s\-]?up\b", "super_built_up"),
    (r"\bplot\b|\bland\b", "plot"),
]


@dataclass(frozen=True)
class ParsedArea:
    area: Area
    raw: str
    basis: str = "unspecified"


def parse_area(text: str) -> ParsedArea | None:
    """Parse an area with its unit, and its measurement basis where stated.

    Basis matters: carpet and super built-up areas for the same flat differ legitimately
    by 30% or more, so comparing across bases is meaningless.
    """
    m = AREA_PATTERN.search(text)
    if not m:
        return None
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0:
        return None

    unit_text = m.group(2).lower()
    unit = next(
        (u for pattern, u in _AREA_UNITS if re.fullmatch(pattern, unit_text, re.IGNORECASE)),
        None,
    )
    if unit is None:
        return None

    # Look for the basis before the number - "Carpet Area: 1150 sq ft".
    prefix = text[: m.start()]
    basis = "unspecified"
    for pattern, name in _AREA_BASIS:
        if re.search(pattern, prefix, re.IGNORECASE):
            basis = name  # later patterns win, so super built-up beats built-up
    return ParsedArea(area=Area.of(value, unit), raw=m.group(0).strip(), basis=basis)


# =====================================================================================
# Names
# =====================================================================================

_HONORIFICS = re.compile(
    r"^\s*(?:shri|sri|smt|smt\.|mr|mrs|ms|dr|late|m/s|messrs)\.?\s+",
    re.IGNORECASE,
)


def normalise_name(raw: str) -> str:
    """Strip honorifics and collapse whitespace.

    Deliberately conservative. Indian names carry transliteration variants, initials,
    patronymics and inconsistent ordering, and collapsing those here would destroy the
    evidence that a match was uncertain. Real matching is a scored operation that belongs
    with entity resolution - this only produces a comparable surface form.
    """
    cleaned = _HONORIFICS.sub("", raw or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:")
    return cleaned
