# Cross-document validation

`src/dmocr/resolve/` · `src/dmocr/pipeline.py` · tests in `tests/test_resolve.py`, `tests/test_pipeline.py`

Cross-document checks only become possible once claims from several documents sit on the
**same entity**. That is what assembly does, and the two resolution decisions it makes are
the whole design.

## Decision 1: one canonical property per case

A case is a loan against one collateral property, so every document's claims attach to a
single `Property`. Documents asserting *different* parcel identifiers or areas therefore
produce **competing claims on the same attribute**, which resolve to `MISMATCH` and
surface as a finding.

The alternative — splitting into separate properties when identifiers disagree — is worse.
The disagreement would vanish into two tidy entities that never get compared, and the case
would look clean. **A conflict must stay visible.**

## Decision 2: parties merge only on a clear name match

The vendor in the Sale Deed and the promoter in the Agreement of Sale should be one party
if the names agree. Where the match is only *partial*, they are kept **separate** and the
decision is recorded.

This asymmetry is deliberate. **False merging is the more dangerous error**: treating two
different people as one can make a broken title chain look continuous. Splitting one person
in two merely raises a spurious mismatch that a reviewer can dismiss.

Every decision is recorded in `AssemblyResult.decisions`, so a reviewer can see why two
names were treated as one person — or not:

```
party.seller: 'SHRI RAMESH PATIL' vs 'R. PATIL'   -> MATCH (0.93) merged
party.buyer:  'SMT. ANITA DESAI'  vs 'ANITA DESSAI' -> MATCH (0.97) merged
```

## Name matching

Indian names vary in ways that defeat string equality:

| Variation | Example | Handling |
|---|---|---|
| Honorifics | Shri, Smt., M/s, Late | stripped |
| Initials | `R. Patil` ↔ `Ramesh Patil` | initial matches first letter, scored 0.85 |
| Transliteration | Desai/Dessai, Anita/Aneeta, Vishwas/Vishvas | phonetic fold |
| Ordering | surname first or last | order-insensitive pairing |
| Patronymics | `Ramesh s/o Ganpat Patil` | parent's name separated, not absorbed |
| OCR damage | `RameshPatil` | compared against the concatenation |

Three outcomes, and the middle one is a real answer:

| Score | Determination |
|---|---|
| ≥ 0.92 | `MATCH` |
| 0.75–0.92 | `PARTIAL_MATCH` — **routes to a human** |
| < 0.75 | `MISMATCH` |

The phonetic fold is deliberately **moderate**. An aggressive one merges genuinely
different surnames — there are tests asserting `patil ≠ patel` and `shah ≠ sharma`.

Unpaired tokens dilute the score, so `Ramesh Patil` vs `Ramesh Ganpat Patil` does not read
as a perfect match.

**None of this is calibrated.** The thresholds are starting points to be tuned against
reviewer outcomes on real documents. Scores are reported so tuning is possible.

## The pipeline

```
files -> ingest -> text extraction -> classify -> extract -> assemble -> rules
```

Deliberately linear and explicit. Reproducibility is a hard requirement, so no model
decides which stage runs next. `ProcessingContext` pins pipeline, rule-set, model versions
and `regulatory_as_of`, so a finding stays explainable under the versions then in force.

Every stage degrades rather than aborting, and each skip is reported:

| Situation | Behaviour |
|---|---|
| Blocked upload (active content) | Not stored, reported; rest of bundle proceeds |
| Rejected by quality gate | Attached and visible, not silently absent |
| Duplicate content | Detected by hash, skipped with a reason |
| Unclassifiable | Routed to human — **never parsed with a guessed schema** |
| No schema for the type | Reported; nothing extracted |

## A bug this stage found

Ownership initially reported *"no instrument capable of transferring title names an
owner"* on a case that **contained a Sale Deed**.

Cause: party claims were attached only to `Party` entities, but ownership checks resolve
against the **property**. The fix attaches party claims to both — "who owns this property"
is a fact about the property, while the `Party` groups name variants for identity
resolution. There is a regression test named for it.

## Worked example

Three documents describing one property, with a deliberate area conflict in the tax bill:

```
DOCUMENTS
  [OK] agreement_of_sale   7 fields
  [OK] property_tax        4 fields
  [OK] sale_deed           9 fields

ENTITY RESOLUTION
  Claims attached: 20 · Parties resolved: 3
  party.seller: 'SHRI RAMESH PATIL' vs 'R. PATIL' -> MATCH (0.93) merged

FINDINGS
  BLOCKER  HIGH      Property area consistent across documents
       2 distinct values across 3 sources; largest agreement group has 2.
  BLOCKER  HIGH      Original title documents held by the lender
  REVIEW   CRITICAL  Ownership established by a title-transferring instrument
       Single source; nothing to corroborate against.
  REVIEW   MEDIUM    Expected collateral documents received
       Missing expected document(s): possession_document
```

Note what *didn't* fire: `MORTGAGE_REG_001` returned `NOT_APPLICABLE` because the security
is an equitable mortgage — the TPA s.59 carve-out working end to end on a real bundle.

## Not yet built

- **Address matching.** Only typed parcel identifiers are compared. Unstructured Mumbai
  addresses need their own component and evaluation set.
- **Title chain validation.** Whether the seller in document N is the buyer in document
  N−1 — the sequencing check that makes ownership continuity provable.
- **Date-ordering checks** across the transaction lifecycle.
- Calibration of the match thresholds against reviewer outcomes.
