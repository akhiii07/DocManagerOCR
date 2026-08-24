"""Field finders.

Each finder takes page text and returns candidate matches with their offsets. Offsets are
a *hint* for grounding — `ExtractionService` re-locates the raw text through
`grounding.ground()` regardless, so a finder cannot emit an ungrounded value even by
mistake.

These are deterministic patterns, covering the high-regularity fields: amounts, dates,
areas, and identifiers. They are the floor, not the ceiling. Fields that need reading
comprehension — recitals of title chain, restrictive covenants, encumbrance narratives —
are left to a model-based extractor that slots in behind the same interface and is subject
to the same grounding control.

Party extraction is the weakest of these and is marked accordingly: it relies on stock
deed phrasing ("hereinafter referred to as the VENDOR") and will miss unconventional
drafting entirely. Missing a party yields no claim, which surfaces as MISSING downstream —
the safe direction.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field as dc_field

from ..model.claims import (
    AreaValue,
    ClaimValue,
    DateValue,
    MoneyValue,
    ParcelValue,
    TextValue,
)
from ..model.common import ParcelIdentifier, ParcelIdentifierType
from . import normalize


@dataclass(frozen=True)
class FieldMatch:
    """One candidate value found in page text."""

    #: Exact text as it appears on the page. This is what gets grounded.
    raw: str
    value: ClaimValue
    start: int
    end: int
    notes: list[str] = dc_field(default_factory=list)


FieldFinder = Callable[[str], list[FieldMatch]]


# =====================================================================================
# Money
# =====================================================================================


def find_consideration(text: str) -> list[FieldMatch]:
    """Amounts in figures, with a figures-vs-words cross-check where words are present."""
    out: list[FieldMatch] = []
    for m in normalize.MONEY_PATTERN.finditer(text):
        parsed = normalize.parse_money_figures(m.group(0))
        if parsed is None:
            continue
        notes: list[str] = []
        if parsed.suspicious_grouping:
            notes.append(
                "Digit grouping matches neither Indian nor Western convention; "
                "possible OCR error in the separators."
            )

        # Words usually follow the figure in parentheses.
        window = text[m.start(): min(len(text), m.end() + 200)]
        check = normalize.cross_check_money(window)
        if check.is_conflict:
            notes.append(
                f"Amount in figures ({check.figures}) does not match the amount in "
                f"words ({check.words_rupees:,}). Requires human review."
            )
        elif check.agree:
            notes.append("Figures and words agree.")

        out.append(FieldMatch(
            raw=m.group(0).strip(),
            value=MoneyValue(amount=parsed.amount),
            start=m.start(),
            end=m.end(),
            notes=notes,
        ))
    return out


#: Words that identify an amount as THE consideration rather than any other figure on
#: the page (stamp duty, registration fee, earnest money, municipal dues).
_CONSIDERATION_ANCHOR = re.compile(
    r"\b(consideration|sale\s+price|purchase\s+price|agreed\s+price|"
    r"total\s+consideration|lump\s*sum)\b",
    re.IGNORECASE,
)
#: An amount stated AFTER the anchor ("Consideration: Rs. X") is the common form and is
#: given the wider window. The trailing form ("Rs. X by way of consideration") is allowed
#: only within a tight window, and only when nothing followed the anchor.
_ANCHOR_FORWARD_WINDOW = 80
_ANCHOR_BACKWARD_WINDOW = 40


def find_consideration_amount(text: str) -> list[FieldMatch]:
    """Amounts anchored to consideration wording.

    Deliberately NOT "the largest amount on the page". A deed states stamp duty,
    registration fees and sometimes earnest money alongside the price, and the largest
    figure is not reliably the consideration.

    Selection is **anchor-driven**: each consideration anchor claims the nearest amount
    that follows it, and only falls back to a preceding amount if nothing follows. The
    naive alternative - "any amount near any anchor" - wrongly picks up an unrelated
    figure that merely happens to sit just before the word, which is exactly what
    "Municipal deposit Rs. 9,99,00,000. Consideration: Rs. 1,25,00,000" produces.

    Unusual wording yields nothing, which surfaces as MISSING - the safe direction -
    rather than a confident wrong number feeding the LTV checks.
    """
    anchors = [m.span() for m in _CONSIDERATION_ANCHOR.finditer(text)]
    if not anchors:
        return []

    amounts = find_consideration(text)
    if not amounts:
        return []

    chosen: dict[int, FieldMatch] = {}  # keyed by match start, to dedupe
    for a_start, a_end in anchors:
        after = [m for m in amounts
                 if m.start >= a_end and m.start - a_end <= _ANCHOR_FORWARD_WINDOW]
        if after:
            best = min(after, key=lambda m: m.start - a_end)
        else:
            before = [m for m in amounts
                      if m.end <= a_start and a_start - m.end <= _ANCHOR_BACKWARD_WINDOW]
            if not before:
                continue
            best = min(before, key=lambda m: a_start - m.end)
        chosen[best.start] = best

    return [chosen[k] for k in sorted(chosen)]


# =====================================================================================
# Dates
# =====================================================================================


def _date_matches(text: str, pattern: re.Pattern[str]) -> list[FieldMatch]:
    out: list[FieldMatch] = []
    for m in pattern.finditer(text):
        parsed = normalize.parse_date(m.group(0))
        if parsed is None:
            continue
        notes = []
        if parsed.ambiguous:
            notes.append(
                f"Date could be read day-first or month-first; "
                f"{parsed.order_assumed} assumed per Indian convention."
            )
        out.append(FieldMatch(
            raw=m.group(0).strip(),
            value=DateValue(value=parsed.value),
            start=m.start(),
            end=m.end(),
            notes=notes,
        ))
    return out


_ANY_DATE = re.compile(
    r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:day\s+of\s+)?[A-Za-z]{3,9}[,\s]+\d{4}\b"
    r"|\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b"
)


def find_dates(text: str) -> list[FieldMatch]:
    return _date_matches(text, _ANY_DATE)


def find_execution_date(text: str) -> list[FieldMatch]:
    """The date the instrument was made, from stock execution phrasing.

    Falls back to nothing rather than to "the first date on the page": the first date in a
    deed is often a recited earlier agreement, not the execution date.
    """
    out: list[FieldMatch] = []
    for m in re.finditer(
        # DOTALL and a plain length window rather than `[^\n.;]`: OCR inserts line breaks
        # mid-sentence, so treating a newline as a sentence boundary truncates
        # "executed ... on the 14th day of\nMarch 2024" to "the 14th day of" and loses
        # the year entirely.
        r"(?:executed|made|entered\s*into)\s*(?:at\s*[\w\s]{0,30}?)?"
        r"(?:on|this)\s*(.{0,70})",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        segment = m.group(1)
        parsed = normalize.parse_date(segment)
        if parsed is None:
            continue
        offset = m.start(1) + segment.find(parsed.raw)
        out.append(FieldMatch(
            raw=parsed.raw,
            value=DateValue(value=parsed.value),
            start=offset,
            end=offset + len(parsed.raw),
            notes=(["Ambiguous date order; day-first assumed."] if parsed.ambiguous else []),
        ))
    return out


# =====================================================================================
# Area
# =====================================================================================


def find_area(text: str) -> list[FieldMatch]:
    out: list[FieldMatch] = []
    for m in normalize.AREA_PATTERN.finditer(text):
        # Parse THIS match for the value and unit. Re-parsing a preceding window would
        # find the earlier area in it, so a second "Carpet Area: 1400 sq ft" on the page
        # would silently report the first one's value.
        parsed = normalize.parse_area(m.group(0))
        if parsed is None:
            continue
        # The basis is stated before the number, so it needs preceding context - but only
        # back to the start of the line, so a previous line's basis cannot leak in.
        line_start = text.rfind("\n", 0, m.start()) + 1
        window_start = max(line_start, m.start() - 60)
        basis_parsed = normalize.parse_area(text[window_start: m.end()])
        if basis_parsed is not None and basis_parsed.basis != "unspecified":
            parsed = normalize.ParsedArea(
                area=parsed.area, raw=parsed.raw, basis=basis_parsed.basis
            )
        notes = []
        if parsed.basis == "unspecified":
            notes.append(
                "Measurement basis not stated (carpet / built-up / super built-up). "
                "Areas on different bases are not comparable."
            )
        out.append(FieldMatch(
            raw=m.group(0).strip(),
            value=AreaValue(area=parsed.area, basis=parsed.basis),  # type: ignore[arg-type]
            start=m.start(),
            end=m.end(),
            notes=notes,
        ))
    return out


# =====================================================================================
# Parcel identifiers
# =====================================================================================


def _parcel_finder(
    pattern: str, id_type: ParcelIdentifierType
) -> FieldFinder:
    rx = re.compile(pattern, re.IGNORECASE)

    def finder(text: str) -> list[FieldMatch]:
        out: list[FieldMatch] = []
        for m in rx.finditer(text):
            raw_id = m.group("id").strip(" .,;")
            if not raw_id:
                continue
            out.append(FieldMatch(
                raw=m.group(0).strip(),
                value=ParcelValue(
                    identifier=ParcelIdentifier(id_type=id_type, value=raw_id)
                ),
                start=m.start(),
                end=m.end(),
            ))
        return out

    return finder


#: Mumbai urban land is keyed by CTS number - see the canonical model notes.
find_cts_number = _parcel_finder(
    r"\bC\.?\s?T\.?\s?S\.?\s*(?:No\.?|Number)?\s*:?\s*(?P<id>[\d]+(?:\s*/\s*[\w]+)?)",
    ParcelIdentifierType.CTS,
)

find_survey_number = _parcel_finder(
    r"\bSurvey\s*(?:No\.?|Number)\s*:?\s*(?P<id>[\d]+(?:\s*/\s*[\w]+)?)",
    ParcelIdentifierType.SURVEY,
)

find_plot_number = _parcel_finder(
    r"\bPlot\s*(?:No\.?|Number)\s*:?\s*(?P<id>[\w\-]+)",
    ParcelIdentifierType.PLOT,
)


# =====================================================================================
# Identifiers
# =====================================================================================


def _text_finder(pattern: str, *, group: str = "id") -> FieldFinder:
    rx = re.compile(pattern, re.IGNORECASE)

    def finder(text: str) -> list[FieldMatch]:
        out: list[FieldMatch] = []
        for m in rx.finditer(text):
            raw = m.group(group).strip(" .,;:")
            if not raw:
                continue
            out.append(FieldMatch(
                raw=raw,
                value=TextValue(raw=raw, normalised=raw.upper().replace(" ", "")),
                start=m.start(group),
                end=m.end(group),
            ))
        return out

    return finder


#: MahaRERA project registration numbers are of the form P<digits>.
find_maharera_number = _text_finder(r"\b(?P<id>P\d{11,})\b")

find_registration_number = _text_finder(
    r"\b(?:Registration|Regn\.?|Doc(?:ument)?)\s*(?:No\.?|Number)\s*:?\s*(?P<id>[\w\-/]+)"
)

find_assessment_number = _text_finder(
    r"\b(?:Assessment|Property\s+Account)\s*(?:No\.?|Number)\s*:?\s*(?P<id>[\w\-/]+)"
)

find_sub_registrar = _text_finder(
    r"\bSub[\s\-]?Registrar\s*(?:of\s+Assurances)?\s*,?\s*(?P<id>[A-Za-z][A-Za-z\s]{2,40})"
)


# =====================================================================================
# Parties
# =====================================================================================

#: Stock deed phrasing. WEAK by design - unconventional drafting will not match, and a
#: missed party surfaces as MISSING downstream, which is the safe direction.
#: `\s*` rather than `\s+` throughout. OCR routinely drops word boundaries - a real
#: recognised line came back as "hereinaftercalledthe" - and requiring whitespace makes
#: the pattern fail on exactly the documents that most need extracting.
#: NOT compiled with a global IGNORECASE flag. The leading `[A-Z]` is what anchors the
#: name to a proper noun, and a global IGNORECASE silently defeats it - the pattern then
#: starts matching at "of the One Part AND ..." and captures connective text as a name.
#: Case-insensitivity is applied only to the fixed keywords, via scoped `(?i:...)`.
_PARTY_PATTERN = re.compile(
    r"(?P<name>[A-Z][A-Za-z.\s]{2,60}?)\s*,?\s*"
    r"(?i:here(?:in)?after)\s*(?i:referred\s*to\s*as|called)\s*(?i:the\s*)?"
    r"[\"'“]?(?P<role>(?i:VENDOR|VENDEE|PURCHASER|SELLER|BUYER|PROMOTER|"
    r"ALLOTTEE|MORTGAGOR|MORTGAGEE|LESSOR|LESSEE|TRANSFEROR|TRANSFEREE))"
)

#: Connective and boilerplate tokens a greedy name group absorbs from deed recitals.
_NAME_STOPWORDS = {
    "and", "of", "the", "one", "other", "part", "parts", "between", "party",
    "parties", "this", "deed", "made", "by", "&", "witnesseth", "whereas",
}

#: Roles that identify the person parting with the property.
SELLER_ROLES = {"vendor", "seller", "transferor", "promoter", "mortgagor", "lessor"}
BUYER_ROLES = {"vendee", "purchaser", "buyer", "allottee", "transferee", "mortgagee", "lessee"}


#: A connective glued to the following name by OCR word-boundary loss:
#: "BETWEENRameshPatil". Only stripped when a capital follows, so "Andrew" survives.
_GLUED_CONNECTIVE = re.compile(r"^(?:BETWEEN|AND)(?=[A-Z][a-z])")


def _trim_name(name: str) -> str:
    """Strip leading recital boilerplate a greedy name capture absorbed.

    "of the One Part AND Anita Desai" -> "Anita Desai". Only LEADING stopwords are
    removed: a trailing one would more likely be part of a business name.
    """
    name = _GLUED_CONNECTIVE.sub("", name)
    tokens = name.split()
    while tokens and tokens[0].strip(".,").lower() in _NAME_STOPWORDS:
        tokens.pop(0)
    return " ".join(tokens).strip(" ,.")


def find_parties(text: str) -> list[FieldMatch]:
    """Named parties with their stated role."""
    out: list[FieldMatch] = []
    for m in _PARTY_PATTERN.finditer(text):
        raw_name = m.group("name")
        name = _trim_name(normalize.normalise_name(raw_name))
        if len(name) < 3:
            continue
        role = m.group("role").lower()
        out.append(FieldMatch(
            raw=raw_name.strip(),
            value=TextValue(raw=name, normalised=name.upper()),
            start=m.start("name"),
            end=m.end("name"),
            notes=[f"role={role}"],
        ))
    return out


def find_seller(text: str) -> list[FieldMatch]:
    return [m for m in find_parties(text)
            if any(n.split("=")[-1] in SELLER_ROLES for n in m.notes)]


def find_buyer(text: str) -> list[FieldMatch]:
    return [m for m in find_parties(text)
            if any(n.split("=")[-1] in BUYER_ROLES for n in m.notes)]
