# Rule engine

`src/dmocr/rules/` · rules in `rules/*.yaml` · tests in `tests/test_rules.py`

## The split

**Policy lives in YAML. Computation lives in Python.**

| YAML (`rules/mvp.yaml`) | Python (`predicates.py`) |
|---|---|
| what is checked, how badly it matters | how to compute the comparison |
| applicability, severity, determinacy | evidence gathering |
| citations, message, recommended action | the determination |
| sign-off status | |

Neither can express the other's part, which is the point. Risk and compliance staff can
read, diff and review the YAML without reading Python; the predicates are reviewed and
tested as code.

### Why not an embedded expression language

CEL or JSONLogic would let rule authors write conditions without touching Python. It was
considered and rejected for now. The conditions this domain needs — comparing claim sets
with tolerance *and* measurement basis, filtering by instrument strength, applying a
conditional registration carve-out — are not one-line comparisons. Expressing them in an
embedded language would either be unreadable or would need so many custom helpers that the
helpers become the real implementation.

A simple expression layer can be added later for trivial conditions without changing the
contract.

## Safety properties, all tested

### 1. Rules ship disabled

Every rule in `rules/mvp.yaml` is `DRAFT` or `PENDING_LEGAL_REVIEW`. **None is
`APPROVED`, so `ENFORCE` mode currently produces zero findings.** That is the safety
property working.

- An `APPROVED` rule without `legal_signoff` is **rejected at load time**.
- `tools/check_regulatory.py` **errors** if an `APPROVED` rule cites a requirement whose
  source is not `PRIMARY_VERIFIED`, or one flagged `REQUIRES_LEGAL_REVIEW`.

Use `DRY_RUN` to evaluate drafts against real cases and measure their false-positive rate
*before* requesting sign-off. Sign-off should be informed by evidence, not by how
reasonable a rule sounds.

### 2. Disposition = severity × determinacy

Severity alone is not enough. A HIGH-severity issue established deterministically from two
documents is a different thing from one a model proposed, and presenting them identically
is how reviewers stop trusting the system.

| | machine-certain | model-proposed |
|---|---|---|
| CRITICAL / HIGH, adverse | `BLOCKER` | `REVIEW_REQUIRED` |
| MEDIUM / LOW, adverse | `REVIEW_REQUIRED` | `REVIEW_REQUIRED` |

And two absolute rules:

- **`NOT_APPLICABLE` is never a finding.** The question did not arise.
- **`NOT_DETERMINABLE` never blocks**, at any severity. We did not establish anything, so
  we must not stop a case on it. It escalates to `REVIEW_REQUIRED` only when the
  underlying issue would be serious if true.

### 3. Regulatory vs business rules are distinguished

`citations` referencing [requirements.yaml](regulatory/requirements.yaml) makes a rule a
**regulatory checkpoint**. An empty list marks a **business rule**, and
`Finding.is_regulatory` is False.

Currently 5 regulatory, 3 business. `XDOC_AREA_001` is a business rule — area consistency
is good underwriting practice, but no instrument we have read mandates it. The review
package must not imply regulatory backing a rule does not have.

This is the same discipline as the `NEG_HFC_NO_TITLE_VERIFICATION_DUTY` finding: the HFC
Master Direction prescribes no title-verification duty, so title checks are business
rules.

### 4. A crashed check is reported, never swallowed

A predicate that raises produces `NOT_DETERMINABLE` with the exception recorded and the
message *"Check could not be completed … This is not a pass."* A check that crashed is a
check that did not happen, and the reviewer is entitled to know.

### 5. Applicability is evaluated before the predicate

An out-of-scope rule yields `NOT_APPLICABLE` with a reason, rather than running and being
ignored. The reviewer sees it as a deliberate non-check.

Effective dates compare against `ProcessingContext.regulatory_as_of`, **not** against
today — reprocessing a case reproduces its original result rather than re-judging it under
current rules.

## The carve-out worth knowing about

`MORTGAGE_REG_001` is the rule most likely to have been written wrong. TPA s.59 requires a
registered instrument for mortgages of ₹100+, **except** mortgage by deposit of title
deeds — which under s.58(f) is available in Bombay and is the dominant Mumbai practice.

| Security type | Result |
|---|---|
| equitable (deposit of title deeds) | `NOT_APPLICABLE` — expressly excepted |
| simple / English / other | `MISSING` if no deed → `BLOCKER` |
| unknown | `NOT_DETERMINABLE` — never a defect |

A blanket "mortgages must be registered" rule would fire on a large share of sound Mumbai
cases. Three tests pin this.

## The highest-value rule

`LTV_CONSIDERATION_001` implements Annex XIV 1.9: the property value used for LTV must not
exceed the documented transaction value in the agreement to sale / sale deed.

It names two of the five MVP document types, needs no external source, and turns an
extracted consideration amount directly into a regulatory check. It also gives
cross-document consistency a *regulatory* purpose — if the documents disagree on
consideration, the checkpoint returns `NOT_DETERMINABLE` rather than silently picking one.

## Running

```bash
python -m pytest tests -q
python tools/check_regulatory.py
```

## Not yet built

- External verification predicates (CERSAI, MahaRERA, IGR) — pending the adapter layer.
- Confidence calibration from reviewer outcomes. `Finding.confidence` currently tracks
  determinacy, which is a placeholder for a calibrated number.
- Finding deduplication and correlation across rules.
