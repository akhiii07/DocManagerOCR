# Architecture Decision Record

Append-only. Each decision records what, why, alternatives, and consequences.
Superseded decisions are marked, not deleted.

---

## ADR-0001 — MVP scope: NBFC/HFC, Mumbai/Maharashtra, five document types
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** MVP targets NBFC/HFC home-loan and loan-against-property underwriting, for
properties in Mumbai / Maharashtra only, across five document types (Agreement of Sale,
Sale Deed, Property Papers, Property Tax, Possession Documents).

**Why.** Narrowing lender type bounds the regulatory research surface (NBFC/HFC-applicable
instruments differ from bank-applicable ones). Narrowing to one state bounds extraction
profiles and verification adapters to one registration system, one RERA authority, one
land-records system and one municipal body.

**Alternatives.** Multi-state from the start (multiplies adapters before the core loop is
proven); lender-agnostic (defers the compliance layer that is a core requirement).

**Consequences.** Applicability predicates are still built into the rule model, but only the
NBFC/HFC and Maharashtra branches are populated. Adding a second state must be treated as a
real project, not a config toggle.

---

## ADR-0002 — No authentication or authorization in MVP
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** No AuthN/AuthZ layer in the MVP. No login, no RBAC, no OIDC.

**Why.** User decision. It removes real scope from the MVP and the platform is internal-only
at this stage.

**Consequences — deliberately mitigated now so this stays reversible:**
1. `tenant_id` and `case_id` remain in the data model and in **every** retrieval filter and
   query predicate. Scoping exists; it is simply not enforced against an authenticated
   principal. Adding auth later becomes middleware + policy, not a schema migration.
2. The audit ledger keeps a mandatory `actor` field. Without auth it records a configured
   operator identity (from env/config) instead of a verified one. The reproducibility
   contract stays intact; only the strength of the identity claim is reduced.
3. API surface must bind to localhost / private network only. An unauthenticated service
   handling collateral documents must never be reachable from an untrusted network.

**Revisit when:** more than one operator uses the system, or before any pilot with real
production cases.

---

## ADR-0003 — Claim-based canonical model (conflicts preserved, not resolved)
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** The canonical property/case model stores multiple simultaneous, possibly
conflicting *claims* per attribute, each attributed to a source. It does not store a single
resolved value. Resolution produces a derived *view*, never a mutation.

**Why.** "Sale Deed says 2400 sq.ft, tax receipt says 2210, MahaRERA says 2400" is the
finding. Flattening to one value with a confidence score destroys exactly the information
cross-document validation exists to surface.

**Alternatives.** Field-with-confidence model (simpler, lossy — rejected).

**Consequences.** More complex model and queries. Every read path must decide explicitly
whether it wants all claims or a resolved view.

---

## ADR-0004 — Span-grounding verifier is mandatory for extraction
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** Any field value produced by a model that cannot be located in the OCR
text/bbox layer of the source document is **discarded** and recorded as `NOT_DETERMINABLE`.

**Why.** This is the primary anti-hallucination control. Confidently wrong extraction is the
highest-damage failure mode in the system because it is silent and confidence-shaped.

**Consequences.** Recall drops relative to unconstrained extraction. This is the correct
trade for an auditable underwriting system: a missing field triggers human review, a
fabricated field triggers a false clearance.

---

## ADR-0005 — External sources classified by access tier; human-operated tier is a designed path
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** Every external verification source is classified T1–T6 (official API →
licensed intermediary → portal-with-permissive-terms → portal-human-operated → offline →
unavailable). T4/T5 human-operated verification is a first-class product feature with a
task queue and evidence capture, not a fallback.

**Why.** Indian property verification is fragmented and largely portal-based, frequently
CAPTCHA-gated, and often subject to terms of use that do not permit automated access.
Scraping is out of scope by explicit requirement. Pretending everything is automatable
would produce an architecture that cannot be built.

**Consequences.** The operator supplies *access*; the system supplies comparison, evidence
capture and audit. A source moving from T4 to T1 later is a config change.

---

## ADR-0006 — Indian authority sources are not reachable from this research environment
**Date:** 2026-08-24 · **Status:** Accepted (observation); mitigation Proposed

**Observations.** Three distinct failure modes across four hosts:

| Host | What it holds | Result |
|---|---|---|
| `www.rbi.org.in` (HTML notifications) | circulars as web pages | **works** — 2 instruments read in full |
| `rbidocs.rbi.org.in` | every Master Direction, as PDF | WAF "Request Rejected" |
| `indiacode.nic.in` | every central and state Act, as PDF | HTTP 403 Forbidden |
| `maharera.maharashtra.gov.in` | RERA project records | connection reset |
| `igrmaharashtra.gov.in` | registration records, Index II | connection reset |

The pattern: **HTML circulars are readable; every PDF-hosted primary instrument is not.**
Search engines can see these documents, but direct retrieval is blocked.

This is why B0 succeeded and B1 cannot proceed from this environment. B0 needed to
*identify* instruments and read a few HTML circulars. B1 needs the *full text* of Master
Directions and Acts, and all of those are PDFs on blocked hosts.

**Consequence for B1 — this is a live blocker, not a caveat.** Deep reading of primary
instruments requires the PDFs to be fetched by other means. These are **public regulatory
documents with no privacy sensitivity**, so the mitigation is simply to download them
locally and read them from disk:

```
docs/regulatory/sources/    (gitignored — public docs, but large and not ours to redistribute)
```

**Consequence for Phase 7 (provisional).** Assume external verification adapters must
egress from Indian infrastructure. Do not design for adapters running from arbitrary
regions. Still **not established** whether the portal failures are deliberate
geo-restriction, generic bot filtering, or transient — verify by retrying from an Indian
network before treating the geo-restriction conclusion as settled. The rbidocs WAF block is
established; the portal geo-restriction hypothesis is not.

---

## ADR-0007 — Canonical model is claim-based, with instrument strength and typed parcel keys
**Date:** 2026-08-24 · **Status:** Accepted · **Implements:** ADR-0003

**Decision.** `src/dmocr/model/` implements the canonical model as claim sets rather than
fields. Three distinctions are load-bearing and were added because B1 findings showed the
simpler shape produces wrong findings:

1. **`InstrumentStrength` on claims** — TPA s.54 means an Agreement of Sale evidences a
   contract, never ownership. `resolve(ownership_only=True)` filters accordingly.
2. **Typed `ParcelIdentifier`** — Mumbai uses CTS numbers, not survey numbers. Different
   types never compare equal.
3. **`SecurityType` gating registration** — TPA s.59 exempts mortgage by deposit of title
   deeds, which is the dominant Mumbai practice.

**Also.** A single claim resolves to `NOT_DETERMINABLE`, not `MATCH` — a lone assertion is
not agreement. Money is integer paise; area is Decimal square metres carrying its
measurement basis.

**Alternatives.** Field-with-confidence (simpler, lossy — rejected in ADR-0003); untyped
identifier strings (rejected: compares incomparable keys); unconditional registration rule
(rejected: false-positive generator for Mumbai).

**Consequences.** More complex reads — every access is a resolution with an explicit
determination. That verbosity is the point. See [canonical-model.md](canonical-model.md).

---

## ADR-0008 — Reject stale or aggregator copies of primary instruments
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** A PDF placed in `docs/regulatory/sources/` is not automatically
authoritative. Every file is graded in `sources.yaml` under `local_copy_provenance`, and
`tools/check_regulatory.py` fails if any file lacks a grade — an ungraded document is an
unverified document.

**Why.** Three supplied files turned out to be aggregator copies self-declaring "this
content could not be verified": the Maharashtra Stamp Act (2019, ~6 years stale, and a
Fourth Amendment landed in 2026), a second Transfer of Property Act copy (version as at
2003), and the Registration Act s.17 extract (Indian Kanoon). Grounding stamp-duty rules
on a 2019 text would have produced confidently wrong compliance results.

**Consequences.** Some requirements stay blocked rather than shipping on weak sources.
`check_regulatory.py` enforces that a requirement may only become a rule when its source is
`PRIMARY_VERIFIED` and it is not flagged `REQUIRES_LEGAL_REVIEW`. Currently 3 of 21 are
blocked. See [OPEN-ITEMS.md](OPEN-ITEMS.md).

---

## ADR-0009 — Rules are YAML policy + named Python predicates, not an expression language
**Date:** 2026-08-24 · **Status:** Accepted · **Supersedes part of:** technology direction (CEL)

**Decision.** Rule specs live in `rules/*.yaml` carrying policy — applicability, severity,
determinacy, citations, message, sign-off. The `check` field names a **registered Python
predicate** that performs the computation. No embedded expression language for now.

**Why.** The earlier direction proposed CEL for rule conditions. On implementing the real
checks, the conditions turned out not to be one-line comparisons: comparing claim sets with
tolerance *and* measurement basis, filtering by instrument strength, applying the TPA s.59
carve-out. In CEL these would either be unreadable or would need so many custom helpers
that the helpers become the implementation — with the downside of being neither reviewable
as YAML nor testable as code.

**Alternatives.** CEL/JSONLogic (rejected for now, revisitable for trivial conditions);
rules entirely in Python (rejected — not diffable or reviewable by risk/compliance staff).

**Consequences.** A new rule shape needs a Python predicate. Policy changes — severity,
applicability, thresholds, message — remain YAML-only. An expression layer can be added
later without changing this contract.

---

## ADR-0010 — Disposition derives from severity AND determinacy
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** `Disposition` is computed from `(determination, severity, determinacy)`.
Only an adverse result that is **machine-certain** and **serious** becomes a `BLOCKER`.
`NOT_DETERMINABLE` never blocks at any severity. `NOT_APPLICABLE` is never a finding.

**Why.** Severity alone conflates "this is bad" with "we are sure". A HIGH-severity issue
a model merely proposed is not the same as one computed deterministically from two
documents, and presenting them identically is how reviewers learn to ignore findings.

**Consequences.** Model-proposed findings can never auto-block, by construction — which is
the intended limit on model authority. Raising a model-proposed issue to blocker status
requires a human.

---

## ADR-0011 — Quality gate degrades rather than rejects; rejected documents stay attached
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** `REJECTED` is reserved for documents that genuinely cannot be processed:
encrypted, unparseable, zero pages, over the page limit. Every other defect — low
resolution, blur, partial text layer, rotation, mixed page sizes — yields `DEGRADED`:
process, but cap confidence. A very low resolution scan is DEGRADED, not REJECTED.

A `REJECTED` document is still attached to the case with its reasons.

**Why.** Real collateral bundles are often poor quality. Rejecting outright pushes work
back to a human with no explanation, and a poor scan is still evidence a reviewer may want
to see. A silent gap in the bundle is worse than a visible failed document.

**Consequences.** Downstream stages must honour `Document.confidence_capped`. The gate
cannot be relied on to guarantee input quality — only to label it.

---

## ADR-0012 — Byte-level safety scan is a filter, not a security boundary
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** `sanitize.scan()` checks magic bytes and PDF capability names, blocking
active content (`/JavaScript`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/GoToR`,
`/SubmitForm`). Blocked content is never stored. Format is decided by magic bytes, not by
the filename.

**Stated limitation.** A PDF can hide object definitions inside compressed object streams,
where these names do not appear in plaintext. A determined adversary can evade this check.
It is recorded as a cheap first filter, **not** a security boundary.

**Why record the limitation rather than fix it.** The fix is not a better parser — it is
sandboxed rendering with no network egress and never executing what a document declares.
Those are deployment controls. Claiming this scan is protection would be worse than the
gap itself, because it would suppress the work of building the real boundary.

**Consequences.** Deployment must supply process isolation and egress control before
production. Tracked in [OPEN-ITEMS.md](OPEN-ITEMS.md).

---

## ADR-0013 — RapidOCR (ONNX PP-OCR) instead of PaddleOCR
**Date:** 2026-08-24 · **Status:** Accepted · **Supersedes:** the OCR recommendation in the
initial technology direction

**Decision.** Use `rapidocr-onnxruntime` — an ONNX packaging of the PP-OCR models — as the
default engine, behind the `OcrEngine` ABC.

**Why the original recommendation failed.** I recommended PaddleOCR. It is not installable
here: **paddlepaddle publishes no wheels for Python 3.14** (`pip` reports "from versions:
none"). This is an environment fact, not a preference.

**Why this package specifically.** There are two RapidOCR distributions:
- `rapidocr-onnxruntime` (1.2.x) — **bundles the models in the wheel**
- `rapidocr` (3.x) — downloads models at runtime, and pulls in `requests`

Under the no-egress privacy constraint, runtime model downloads are outbound network
activity on a machine that is supposed to have none. The self-contained wheel is the
requirement, not a convenience.

**Alternatives.** Tesseract (weaker on degraded scans); docTR (heavier dependency tree);
downgrading the project to Python 3.11 to reach paddlepaddle (rejected — the engine is
replaceable, the interpreter version is not a good thing to pin to an optional dependency).

**Consequences.** CPU-only via onnxruntime as installed; ~4 s/page on the test fixture. GPU
execution needs a different onnxruntime build. Accuracy on real Indian legal documents is
**unmeasured** — see OPEN-ITEMS. Swapping engines touches one adapter.

**Revisit when:** GPU hardware is available, or a real corpus shows PP-OCR underperforming
on Mumbai deeds.

---

## ADR-0014 — One canonical property per case; parties merge only on a clear name match
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** Case assembly attaches every document's claims to a **single** `Property`.
Party claims attach to both the property and a resolved `Party`. Two parties merge only on
`MATCH`; a `PARTIAL_MATCH` keeps them separate and records the decision.

**Why one property.** Documents asserting different identifiers or areas then produce
competing claims on the same attribute, which resolve to `MISMATCH` and surface as a
finding. Splitting into separate properties on disagreement would make the disagreement
vanish into two tidy entities that never get compared, and the case would look clean.

**Why the merge asymmetry.** False merging is the more dangerous error: treating two
different people as one can make a broken title chain look continuous. Splitting one person
in two merely raises a spurious mismatch a reviewer can dismiss. So the uncertain band
routes to a human rather than deciding.

**Consequences.** Every resolution decision is recorded for audit. Match thresholds are
uncalibrated starting points — see OPEN-ITEMS 33.

**Bug this decision corrected:** party claims initially attached only to `Party` entities,
so ownership checks — which resolve against the property — reported "no instrument capable
of transferring title names an owner" on a case containing a Sale Deed.

---

## ADR-0015 — Verification source registry is loaded from the B0 research, not coded
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** `dmocr.verify.sources` reads `docs/regulatory/sources.yaml` — the file the
B0 authority-map research produced — for tiers, verified attributes and lookup keys. A tier
recorded as a range (`T1_OR_T2`) resolves to the **worse** tier.

**Why.** Duplicating the registry in code would let a source's tier drift from the research
that established it, and the research is the thing a reviewer would be shown if asked why
a source was trusted. Resolving ranges pessimistically avoids promising automation the
environment cannot deliver — B0 recorded these tiers as preliminary with low confidence.

**Consequences.** Adding a source is a research-file edit. The code must tolerate research
vocabulary it does not recognise, so `_VERIFIES_TO_ATTRIBUTE` is an explicit map and
unmapped terms are skipped rather than guessed.

---

## ADR-0016 — SOURCE_UNAVAILABLE is never a compliance failure
**Date:** 2026-08-24 · **Status:** Accepted

**Decision.** Only `MISMATCH` and `NOT_FOUND_IN_SOURCE` are adverse verification outcomes.
`SOURCE_UNAVAILABLE`, `PENDING_MANUAL`, `NOT_APPLICABLE` and `STALE` are not, and
`checks_performed` counts only results where a source actually answered.

**Why.** A portal being down says nothing about the collateral. Conflating "we could not
check" with "the check failed" would make the system untrustworthy in the first direction
reviewers notice, and it is the single easiest mistake to make in this layer.

**Consequences.** Case *completeness* must be reported separately from pass/fail, and the
review package has to show open items rather than burying them in a pass rate.

---

## Open decisions

| ID | Decision | Blocks | Notes |
|---|---|---|---|
| O2 | Temporal vs lightweight queue for workflow orchestration | Phase 1 | Recommendation is Temporal (durable execution, workflow versioning, replayable history = free audit trail). Counter-argument: real operational weight. Decide once the human-wait-state load in the verification layer is visible. |
| O3 | PyMuPDF (AGPL) vs pypdfium2+pdfplumber (permissive) | Phase 3 | PyMuPDF is technically better; AGPL needs a commercial licence for a commercial lending product. Raise with legal early. Corpus survey uses pypdfium2 to avoid pre-committing. |
| O4 | Item 3 of the user's 2026-08-24 scope message | — | Message was truncated; content unknown. |
