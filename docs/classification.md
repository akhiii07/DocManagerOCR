# Document classification

`src/dmocr/classify/` · tests in `tests/test_classify.py`

## Why UNKNOWN is a feature

Classification decides which extraction schema applies. A wrong answer doesn't produce an
obvious error — it produces a **full set of confidently wrong fields**, because a Sale Deed
parsed as a Property Tax receipt still yields *something*.

So the classifier is tuned to reach for human review rather than guess. Three distinct
ways to land on `UNKNOWN`, each reported separately so a reviewer knows what happened:

| Reason | Meaning |
|---|---|
| `NO_TEXT` | Nothing to classify on. A scanned document needs OCR first. |
| `WEAK` | Best candidate didn't clear `min_score`. |
| `AMBIGUOUS` | Two candidates too close to separate. |

## The cross-reference problem

This is the failure mode that makes naive keyword matching useless here.

A **Sale Deed routinely recites the Agreement of Sale** it supersedes: *"AND WHEREAS by an
agreement for sale dated 2nd January 2024…"*. A Possession Letter names the agreement and
the flat. Keyword presence alone points the wrong way on a large share of real bundles.

Two mitigations, both tested:

**1. Position weighting.** A phrase in the title region of page 1 is far more indicative
than the same phrase on page 14.

| Page | Weight factor |
|---|---|
| 1 | 1.0 |
| 2 | 0.5 |
| 3+ | 0.15 |

Signals marked `title_only` (document titles like "DEED OF SALE") fire **only** in the
first 1200 characters of page 1. Anywhere else they're a recital, and are ignored entirely
rather than discounted.

**2. Per-signal contribution caps.** A signal contributes at most 2× its weight however
often it repeats, so a verbose document can't swamp the score.

## Why rules rather than a model

Three reasons, and the third is the honest one:

- **Auditable** — a decision names the phrases that produced it and the pages they were
  on. A reviewer can disagree with a specific piece of evidence.
- **Cheap to correct** — a misfire is a weight adjustment, not a retraining cycle.
- **There is no labelled corpus** — a supervised classifier trained on synthetic documents
  would learn the generator, not the domain.

This is a **baseline, not the end state.** Once a real labelled Mumbai corpus exists, a
supervised classifier should be measured against it. The rule layer stays useful as a prior
and as a sanity check on the model.

## Signals worth noting

**MODT** — Memorandum of Deposit of Title Deeds. Recognising it matters beyond
classification: it identifies the security as an equitable mortgage, and `SecurityType`
gates the TPA s.59 registration check (`REQ_TPA_59`). Misreading a MODT as a mortgage deed
would flip that rule from `NOT_APPLICABLE` to a defect.

**Promoter / Allottee** — RERA vocabulary, indicating an agreement for an
under-construction unit rather than a completed conveyance.

**`PROPERTY_PAPERS` has no signals at all** and is never auto-assigned. It is a catch-all
label for a bundle, not a recognisable document. Auto-assigning it would let anything
unrecognised acquire a schema.

## The Marathi lexicon is unvalidated

Devanagari signals are included at **low weights** so they can corroborate but never decide
a classification alone — there's a test pinning that `खरेदीखत` on its own yields `UNKNOWN`.
The whole set can be disabled with `ClassifierConfig(use_devanagari=False)`.

**These terms have not been checked against real Maharashtra instruments by a Marathi
reader.** Tracked in [OPEN-ITEMS.md](OPEN-ITEMS.md) — the lexicon needs review before its
weights go up.

## Interaction with quality

A `DEGRADED` document's text came from a poor scan, so classification rests on unreliable
input however decisive the phrases look. Confidence is capped at `MEDIUM` — but the *type*
is unchanged, since capping confidence and changing the answer are different things.

## Human classifications outrank the classifier

`apply_to_document()` refuses to overwrite a type that was set deliberately — declared at
upload or corrected by a reviewer — unless explicitly asked.

## Not yet built

- OCR, so scanned documents currently classify as `UNKNOWN / NO_TEXT`. That is honest
  rather than broken: there is genuinely nothing to classify on.
- Visual/layout classification for documents whose text is uninformative.
- Multi-document splitting. A single PDF containing a deed *and* its annexures is treated
  as one document.
- Measurement. There are no accuracy numbers because there is no labelled corpus. The
  tests pin *behaviour*, not accuracy.
