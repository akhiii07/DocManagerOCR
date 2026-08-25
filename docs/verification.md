# External verification

`src/dmocr/verify/` · tests in `tests/test_verify.py`

```
Case (assembled)
  -> PLANNER      which authorities are in scope, at what tier, what minimum data to send
  -> ADAPTERS     automated (T1–T3) call out; T4/T5 become operator tasks
  -> COMPARE      external observation vs internal claims, same semantics as cross-doc
  -> RESULTS      MATCH · PARTIAL_MATCH · MISMATCH · NOT_FOUND · UNAVAILABLE · N/A · STALE · PENDING
```

## The invariant that matters most

**`SOURCE_UNAVAILABLE` is never a compliance failure.** A portal being down says nothing
about the collateral. Conflating "we could not check" with "the check failed" is the single
easiest mistake to make in this layer, and it would make the system untrustworthy in the
first direction reviewers notice.

Only `MISMATCH` and `NOT_FOUND_IN_SOURCE` are adverse. Unavailable sources and pending
tasks contribute to **case completeness**, reported separately from pass/fail —
`run.summary()` has a `checks_performed` count that only rises when a source actually
answered.

## The registry comes from the B0 research, not from code

`sources.py` loads `docs/regulatory/sources.yaml` — the file the authority-map research
produced. Tiers, what each source verifies, and what it is keyed by cannot drift from the
research that established them.

A tier recorded as a range (`T1_OR_T2`) resolves to the **worse** tier. B0 recorded these
as preliminary with low confidence, and planning on the optimistic end would promise
automation the environment cannot deliver.

| Source | Tier | Automatable |
|---|---|---|
| CERSAI | T2 | **yes** |
| IGR eSearch | T4 | no — blocked on terms of use |
| MahaRERA | T4 | no — blocked on terms of use |
| MCGM property tax | T4 | no — CAPTCHA |
| Property Card | T5 | no — application-based |

One of five. That ratio is why the next section exists.

## T4/T5 is the primary mechanism, not a fallback

The operator supplies **access**; the system supplies **comparison, evidence capture and
audit**. A task carries exactly what to send and nothing more, and its result re-enters
through `ingest_manual_observation`, which uses the **same comparison path** as an
automated result.

That symmetry is the point: moving a source from T4 to T1 later is a configuration change,
not a rewrite.

```
[SRC_PROPERTY_CARD_MH] Maharashtra Land Records Department / City Survey Office
  Retrieve the record for: cts_number=1234/5A
  Capture: party.owner, property.parcel_identifier, property.area
  Send only the identifiers listed above - do not widen the query.
```

`UNOBTAINABLE` is a distinct task outcome from `COMPLETED`: the operator tried and could
not retrieve it. Not a check, not a failure.

## Data minimisation is part of planning

An external lookup is an **outbound disclosure of customer data**, not a neutral read. The
planner sends **one** key per source — the narrowest that resolves a record, taken from
`keyed_by` which is ordered best-first. If a CTS number alone retrieves a Property Card,
the owner's name is not sent.

What was sent is recorded on the `Snapshot`, so the disclosure is auditable.

**A disputed key is never used.** If documents disagree on the CTS number, querying on the
majority reading would retrieve the wrong record and produce a confidently wrong
verification. The key is skipped and the ambiguity reported.

## Applicability gates prevent manufactured findings

**MahaRERA is `NOT_APPLICABLE` without a registration number.** Much of Mumbai's older
resale stock has no RERA record at all, and `REQ_RERA_3_2_REGISTRATION_EXEMPTION` remains
`REQUIRES_LEGAL_REVIEW`. Absence therefore defaults to not-applicable, never a finding.

**Out-of-scope sources are reported, not dropped** — the reviewer needs to see that a
source was considered and why it did not apply.

## Comparison reuses cross-document semantics

Area tolerance and measurement basis, typed parcel keys, scored name matching. "The deed
disagrees with the tax bill" and "the deed disagrees with the Property Card" are judged the
same way. Two behaviours are specific to external comparison:

**Containment yields `PARTIAL_MATCH`.** A deed naming "ABC Residency" against a RERA record
for "ABC Residency Phase II" is the *expected* shape of a phased project
(`REQ_RERA_3_EXPLANATION_PHASE_IS_STANDALONE`), not a contradiction. Reporting it as
`MISMATCH` would fire on every phased development.

**A disputed internal value downgrades a match.** If our own documents disagree, an
external record agreeing with the majority reading confirms only one of the competing
readings — so `MATCH` becomes `PARTIAL_MATCH`.

**The tier caps confidence.** A statutory API and an operator's screenshot do not carry the
same weight, however clean the comparison looks.

## Snapshots

Every retrieval produces an immutable `Snapshot`: authority, tier, `retrieved_at`,
operator id where human-mediated, the request keys, and a reference to the stored artefact.

External data is snapshotted and **never re-fetched during a re-run**. Without that a case
cannot be reproduced, because the outside world will have moved — and a finding that cannot
be reproduced cannot be defended.

## Results become findings

Verification results are written onto the `Case`, and rules read them through the same
engine as everything else — no parallel reporting path. Two predicate shapes, because
comparison and presence are different questions (ADR-0017):

| Predicate | Question | Used by |
|---|---|---|
| `external_agreement` | Does the authority agree with the documents? | owner, area |
| `external_record_presence` | Does the register hold a record at all? | prior charge |
| `verification_coverage` | How much of the plan actually happened? | completeness |

**`EXT_CERSAI_CHARGE_001` is the one rule where absence is the good answer.** A record
existing means a prior charge over the collateral, which under SARFAESI s.26C takes
priority over our security. The reading is a parameter (`presence_means: adverse`) rather
than an assumption, so the inversion is visible in the rule file.

```
no adapter registered  ->  NOT_DETERMINABLE   REVIEW_REQUIRED
no charge on register  ->  MATCH              CLEARED
prior charge exists    ->  MISMATCH           BLOCKER  (CRITICAL)
```

### The bug that shape prevented

A CERSAI hit arrives as `NOT_APPLICABLE`, because there is nothing in the borrower's own
documents to compare a charge against. The first implementation filtered those out as
"not a real answer" — so **a genuine prior charge reported `NOT_DETERMINABLE` and was
effectively invisible.**

Fixed by making `counts_as_a_check` mean *"the authority answered"*: `NOT_APPLICABLE` with
an external value counts, because the register holding a record **is** the signal. Two
regression tests name this.

## Not yet built

- **A real CERSAI adapter.** It is the one plausible automated source and has the
  strongest legal footing (SARFAESI s.26: inspection open to any person, expressly
  including electronically). Blocked on whether the lender holds an entity account
  (OPEN-ITEMS 7) and on ADR-0006. Everything downstream of it is built and tested.
- **Artefact storage** for operator captures — currently a reference only.
- **Retry and rate-limit policy** per source.
- Freshness windows are placeholders, not researched.
