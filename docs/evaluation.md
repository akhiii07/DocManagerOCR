# Evaluation harness

`src/dmocr/eval/` · CLI `tools/evaluate.py` · tests in `tests/test_eval.py`

```bash
python tools/evaluate.py --truth eval/groundtruth --documents fixtures --gates
```

## The metrics must not reward guessing

This is the design decision everything else follows from.

A naive harness scores every non-correct answer as an error. Under that scoring, a system
that **guesses beats a system that says `UNKNOWN`** — and every control this platform has
against confident wrongness becomes a liability in its own evaluation.

So outcomes separate *being wrong* from *declining to answer*:

| Outcome | Meaning | |
|---|---|---|
| `CORRECT` | matched the reference | |
| `NEAR` | plausible, not acceptable — routes to a human | safe |
| `WRONG` | produced a different answer | **dangerous** |
| `MISSING` | reference has a value, system produced none | safe |
| `SPURIOUS` | invented a value the reference says doesn't exist | **dangerous** |
| `NOT_EVALUATED` | no reference to compare against | — |

**`dangerous_error_rate` is the headline safety metric.** A system can have mediocre
recall and still be trustworthy. It cannot have a high dangerous-error rate and be
trustworthy, because those failures reach a Risk Manager *as an answer* rather than as a
gap.

There's a test asserting the property directly: a guesser scores better on recall than a
cautious system, and worse on danger.

## Classification: deferral is not error

`UNKNOWN` predictions are excluded from accuracy and reported as a **deferral rate**.
Counting a deliberate "route this to a human" as a misclassification would score a guessing
classifier above a cautious one — and a wrong document type produces a full set of
confidently wrong fields.

Three separate numbers: `accuracy_on_decided`, `deferral_rate`, `misclassification_rate`.

## `absent_fields` makes invention visible

Ground truth can assert that a field is **not** present:

```yaml
absent_fields:
  - maharera_number     # no RERA number appears in this deed
```

Without it, a value invented where none exists is indistinguishable from an unlabelled
field, and the harness silently ignores it. Same for `expected_clear` on rules: **false
positives are invisible without it**, and a rule set that fires on everything scores
perfectly on recall alone.

## Privacy

Ground truth for real documents is **transcribed customer data** — owner names,
consideration amounts, sometimes whole pages of reference text. It is customer content:

- it lives **outside the repository** (the loader warns if it doesn't, unless marked
  `synthetic: true`)
- reports carry **metrics and identifiers only, never values**
- `--show-values` is local debugging only, and the output becomes customer content

That last point is not incidental. A report nobody can circulate is a report nobody reads,
so the default output is safe to share.

## What the synthetic corpus actually tells us

Running against generated fixtures gives 100% extraction precision and recall. **That
measures plumbing, not competence.** The values are the ones the generator wrote.

The one genuinely informative number is OCR:

```
CER mean: 4.2%     WER mean: 29.8%
```

**Characters read well; word boundaries do not.** That 7× gap is exactly the failure that
broke four extraction patterns during the previous phase, and it's now a tracked metric
rather than an anecdote. A test pins the relationship.

Note the report leads with **coverage** and flags small corpora, because a rate over four
documents is indicative and should never be quoted as an accuracy figure.

## Regression gates

```
[PASS] extraction dangerous errors: 0.0 <= 0.05
[PASS] extraction recall: 1.0 >= 0.7
[PASS] classification misclassification: 0.0 <= 0.05
[PASS] finding false positives: 0 <= 0
```

An **unmeasured metric skips by default** rather than failing. "Not measured" is not
"failed", and a gate that failed on an unmeasurable metric would push toward labelling data
purely to make CI pass. Set `required=True` where absence really is a failure.

The thresholds are **uncalibrated**. They exist so a regression is visible, not because
these numbers have been shown to be right. Tighten them against a real corpus rather than
treating them as targets already met.

## Ground truth format

```yaml
synthetic: true              # omit for real documents
case_id: GT_001
documents:
  - file: bundle/sale_deed.pdf
    document_type: sale_deed
    fields:
      consideration: "12500000"
      area: {value: 1150, unit: sq_ft, basis: carpet}
      seller: "Ramesh Patil"
    absent_fields: [maharera_number]
    reference_text: |        # optional; enables CER/WER
      DEED OF SALE ...
expected_findings:
  - {rule_id: XDOC_AREA_001, determination: MISMATCH}
expected_clear:
  - MORTGAGE_REG_001         # must NOT fire adversely
```

Value matching reuses the platform's own semantics — area tolerance and basis, typed parcel
keys, scored name matching — so the harness judges a value the same way the system does. A
harness with its own notion of equality would measure something other than the product.

## Not yet built

- **A real labelled corpus.** The harness is the easy half; transcribing ground truth for
  real Mumbai deeds is the expensive half, and nothing here substitutes for it.
- **Reviewer-outcome metrics** — agreement, correction rate, review time. These need the
  Risk Manager UI and real usage.
- **Confidence calibration** measured against outcomes.
- **Stratified reporting** by scan quality, document age and script, which is where the
  interesting variation will be.
