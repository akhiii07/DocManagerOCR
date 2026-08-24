# Canonical data model

`src/dmocr/model/` · tests in `tests/test_model.py`

Read this before changing the model. Several distinctions in it look redundant and are
not — each exists because a specific regulatory finding in
[requirements.yaml](regulatory/requirements.yaml) says a simpler shape would produce
**wrong findings on real Mumbai cases**.

---

## The central idea: claims, not fields

A `Property` has no `area` attribute. It has a **claim set** for `property.area` that may
hold several disagreeing assertions, each with its own provenance and confidence.

```
Property
 └── claim_sets
      └── "property.area"
           ├── Claim  2400 sq ft   ← Sale Deed, p4, bbox
           ├── Claim  2400 sq ft   ← MahaRERA, retrieved 2026-08-24
           └── Claim  2210 sq ft   ← Property Tax receipt, p1, bbox
```

Asking "what is the area?" is a **resolution** producing a `Resolution` *view* — never a
mutation, never a discarded claim.

**Why.** The disagreement *is* the finding. A field-with-confidence model would store one
value and silently lose the other two, destroying exactly the information cross-document
validation exists to surface. (ADR-0003)

Resolution semantics, all tested:

| Situation | Determination | Note |
|---|---|---|
| No claims | `MISSING` | |
| One claim | `NOT_DETERMINABLE` | **A lone assertion is not agreement.** A value is still offered, but it must never read as corroborated. |
| All agree | `MATCH` | confidence `HIGH` |
| Disagreement | `MISMATCH` | Majority value offered as a starting point; **every dissenting claim id carried alongside**. |

---

## Five decisions with regulatory grounding

### 1. Instrument strength on ownership claims
`REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST` — TPA s.54: *"A contract for the sale of
immoveable property ... does not, of itself, create any interest in or charge on such
property."*

So a buyer named in an **Agreement of Sale is a prospective purchaser, not an owner**.
Every document-sourced claim carries an `InstrumentStrength`, and
`resolve(ownership_only=True)` filters to claims capable of establishing ownership.

Without this, a case holding only an Agreement of Sale would appear to establish title —
because the buyer's name would sit in the same `owner` slot as a Sale Deed's transferee.
Unmapped document types default to `NON_PROBATIVE`, never to probative.

### 2. Typed parcel identifiers
Mumbai urban land is keyed by **CTS number** on the Property Card; rural Maharashtra uses
survey / gat / hissa. `ParcelIdentifier` carries its type and an optional locality.

`CTS 145` and `Survey 145` are unrelated parcels that share digits, and their comparable
keys differ. Two identical CTS numbers in different villages are also different parcels,
which is what `locality` is for. A single `survey_number: str` would compare incomparable
identifiers and break on the first non-Mumbai district.

### 3. Security type gates the registration check
`REQ_TPA_59` — a mortgage of ₹100+ *"other than a mortgage by deposit of title-deeds"*
requires a registered instrument. `REQ_TPA_58F` — Bombay is a named town where mortgage by
deposit of title deeds is available, and it is the dominant Mumbai practice.

So `SecurityType.requires_registered_instrument` is **conditional**, and
`Case.mortgage_requires_registration()` returns `None` — not `False` — when the security
type is unknown. The honest answer there is `NOT_DETERMINABLE`; asserting a registration
defect without knowing the security type would be a false positive.

A blanket "mortgages must be registered" rule would fire on a large share of sound Mumbai
cases.

### 4. Custody status is first-class
`INST_RBI_RELEASE_DOCS_2023` requires release of originals within 30 days of full
repayment at ₹5,000/day for delay. And in an equitable mortgage the **originals held
*are* the security**.

Both require the lender to know exactly which originals it holds per case, so
`Document.custody` and `Case.custody_inventory()` are part of the model rather than
bookkeeping bolted on later.

### 5. Area carries its measurement basis
Carpet, built-up and super built-up areas for the same flat legitimately differ by 30%+.
`AreaValue.basis` is compared before magnitudes, and claims with incompatible bases
**never agree** regardless of how close the numbers are.

---

## Value handling

**Money is integer paise. Never float.** Consideration amounts drive the LTV computation
(`REQ_HFC_19_1_LTV_COMPUTATION`) and the Annex XIV 1.9 cap. Binary floating point cannot
represent 0.1 exactly, and the resulting drift would surface as a spurious cross-document
mismatch.

**Area is Decimal square metres**, with exact conversion factors, keeping the original
figure and unit alongside. The reviewer needs to see "2400 sq.ft" as written; the
comparison engine needs one canonical unit. Default tolerance is 2% — a starting point to
be tuned against reviewer outcomes, not a derived constant.

---

## Provenance

Four origins, deliberately not interchangeable: `document`, `external`, `human`, `derived`.

- `DocumentProvenance` carries page, bbox, text span and OCR confidence. `ocr_confidence
  is None` means the value came from an embedded text layer — more reliable than OCR.
  The span is what the span-grounding verifier checks against (ADR-0004).
- `ExternalProvenance` requires `retrieved_at` and `snapshot_id`. External data is
  **snapshotted, never re-fetched** during a re-run, or the case cannot be reproduced.
- `HumanProvenance.supersedes` records what was overridden. Corrections are **new claims,
  never edits** — `active_claims()` filters superseded ones while the originals remain for
  audit.
- `DerivedProvenance` requires a non-empty `input_claim_ids`. A derived value that cannot
  name its inputs is not traceable.

`ProcessingContext` pins pipeline, rule-set, model and prompt versions plus
`regulatory_as_of`, so a finding remains explainable months later under the rules then in
force.

---

## Determination is five-valued

`MATCH · PARTIAL_MATCH · MISMATCH · MISSING · NOT_APPLICABLE · NOT_DETERMINABLE`

Only `MISMATCH` and `MISSING` are adverse. `NOT_DETERMINABLE` (we lacked evidence) and
`NOT_APPLICABLE` (the question does not arise) are **not failures**. Collapsing either
into a failure is the single most likely source of false positives here — and it is
exactly the trap the RERA exemption logic sets, where much of Mumbai's older resale stock
has no RERA record at all and correctly returns `NOT_APPLICABLE`.

---

## Not yet built

- Name and address matching (transliteration, initials, honorifics). `Party.name_variants`
  records every surface form; matching is a scored operation that will live elsewhere and
  needs its own adversarial evaluation set.
- Findings, rules and confidence scoring.
- Persistence. The model is pure Pydantic with no storage coupling.
