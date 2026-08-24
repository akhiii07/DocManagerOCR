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

## Open decisions

| ID | Decision | Blocks | Notes |
|---|---|---|---|
| O2 | Temporal vs lightweight queue for workflow orchestration | Phase 1 | Recommendation is Temporal (durable execution, workflow versioning, replayable history = free audit trail). Counter-argument: real operational weight. Decide once the human-wait-state load in the verification layer is visible. |
| O3 | PyMuPDF (AGPL) vs pypdfium2+pdfplumber (permissive) | Phase 3 | PyMuPDF is technically better; AGPL needs a commercial licence for a commercial lending product. Raise with legal early. Corpus survey uses pypdfium2 to avoid pre-committing. |
| O4 | Item 3 of the user's 2026-08-24 scope message | — | Message was truncated; content unknown. |
