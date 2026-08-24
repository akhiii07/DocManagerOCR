# Structured extraction

`src/dmocr/extract/` · tests in `tests/test_extract.py`

```
OcrDocument + document type
  -> select schema          no schema -> nothing extracted, and it says so
  -> run each field's finder over the pages in scope
  -> GROUND every candidate ADR-0004: unlocatable values are discarded
  -> emit Claims with DocumentProvenance (page, span, bbox, confidence)
```

## Span grounding is structural, not a policy

**A value that cannot be located in the document's extracted text does not become a
claim.** This isn't a check that runs at the end — it's the only way a claim can be
constructed. `ExtractionService` builds claims exclusively through a path that requires a
`DocumentProvenance`, and provenance can only be produced by locating the value in page
text. There is no code path that emits an ungrounded claim.

`ground()` **raises** rather than returning `None`, deliberately. Returning `None` invites
`provenance or some_default`, which is exactly how ungrounded values get emitted.

Matching tolerates whitespace differences (OCR spacing varies) but **not differing
characters** — a value whose digits don't appear on the page is rejected.

## Schema shape carries law

The **Agreement of Sale schema has no `owner` field.** That's not an oversight:

> TPA s.54 — *"A contract for the sale of immoveable property ... does not, of itself,
> create any interest in or charge on such property."*

An allottee is a prospective purchaser, not an owner. If that field existed, a case
holding only an Agreement of Sale would appear to establish title. The Sale Deed schema
*does* have it. There's a test for both.

`PROPERTY_PAPERS` has no schema at all — it's a catch-all label, not a recognisable
document.

## Indian conventions

**Digit grouping.** `1,25,00,000` is two-two-three, not `12,500,000`. Both parse to the
same number, but *grouping style* is an OCR sanity signal: a figure matching neither
convention is flagged as likely-misread separators.

**Amounts in words.** Deeds state consideration twice — `Rs. 1,25,00,000/- (Rupees One
Crore Twenty Five Lakh only)`. Both are parsed and compared. **A figures-vs-words mismatch
is an integrity signal**, not a formatting quirk, and it caps the field's confidence to
`LOW`.

**Day-first dates.** The Indian convention, and the default. Where both components are
≤ 12 the reading is marked `ambiguous` rather than silently resolved, so a reviewer can be
told an assumption was made. `03/14/2024` can only be month-first and is handled.

## Consideration is anchored, not "largest amount wins"

A deed states stamp duty, registration fees and sometimes earnest money alongside the
price. The largest figure is not reliably the consideration.

Selection is **anchor-driven**: each consideration anchor claims the nearest amount that
*follows* it, falling back to a preceding one only if nothing follows. The naive
alternative — "any amount near any anchor" — wrongly picks up a figure that merely happens
to sit just before the word:

```
Municipal deposit Rs. 9,99,00,000. Consideration: Rs. 1,25,00,000
                     ^ wrongly selected by a proximity rule
```

Unusual wording yields **nothing**, surfacing as `MISSING` — the safe direction — rather
than a confident wrong number feeding the LTV checks.

## Internal contradictions become competing claims

Two different areas in one document produce **two claims**, not a resolved value. An
internal contradiction then surfaces through the same machinery as a cross-document one.
Exact repeats collapse.

## What real OCR taught us

Running the real engine over the rendered fixture produced this:

```
March2024BETWEENRameshPatil,hereinaftercalledthe
```

**OCR drops word boundaries.** Four patterns failed on it, and all four fixes are genuine
robustness improvements rather than fixture-specific hacks:

| Bug | Fix |
|---|---|
| `(\d{4})\b` rejected `March2024BETWEEN` — no boundary between `4` and `B` | trailing `(?!\d)` |
| `[^\n.;]` treated an OCR line break as a sentence end, truncating the date to "the 14th day of" | length window with `DOTALL` |
| `\s+` between words failed on `hereinaftercalledthe` | `\s*` throughout |
| A global `re.IGNORECASE` defeated the leading `[A-Z]` name anchor, so the pattern matched connective text | scoped `(?i:...)` on keywords only |

Each has a regression test. This is the clearest evidence so far that **extraction quality
cannot be assessed on synthetic data** — these were found by running one real recogniser
over one generated page.

One artifact remains and is not worth contorting patterns for: the seller extracts as
`rameshpatil` without a space, because OCR glued the tokens and nothing short of a word
splitter recovers it.

## Deterministic only, for now

These are pattern-based finders covering high-regularity fields: amounts, dates, areas,
identifiers, and parties via stock deed phrasing. **Party extraction is the weakest** and
is marked so — unconventional drafting yields nothing, which surfaces as `MISSING`.

Fields needing reading comprehension — title chain recitals, restrictive covenants,
encumbrance narratives — are left to a model-based extractor that slots in behind the same
`FieldFinder` interface and is subject to **the same grounding control**. That is the point
of making grounding structural: a model extractor gains no ability to assert something the
page doesn't support.

## Not yet built

- Model-based extraction for semantic fields.
- Entity resolution across documents (name/address matching is a scored operation with its
  own adversarial evaluation set).
- Table extraction, for property tax schedules.
- Any accuracy measurement — precision/recall per field needs a labelled corpus.
