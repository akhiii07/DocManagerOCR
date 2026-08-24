# Open items

Everything deferred, in one place, so it is not rediscovered by accident. Updated 2026-08-24.

Decision on 2026-08-24: **proceed with building; these do not block the pipeline.** Files
will be supplied if and when they become available.

---

## Blocked on documents we do not have

| # | Item | What it blocks | Impact of proceeding without it |
|---|---|---|---|
| 1 | **Registration Act 1908, full text from India Code** (esp. **s.49**, effect of non-registration) | `REQ_REG_17_1_B_COMPULSORY_REGISTRATION`, `REQ_REG_17_1A_AGREEMENT_OF_SALE_REGISTRATION` — both recorded but **not rule-ready** | We can detect a missing registration but cannot state its legal consequence. Findings must say "registration particulars absent", not "the instrument is void". |
| 2 | **Maharashtra Stamp Act 1958, current official text** | All stamp-duty checks | **No stamp-duty requirement has been extracted and none may be.** Local copy is a 2019 aggregator text; a Fourth Amendment in 2026 confirms it is actively amended. Stamp adequacy is simply out of scope until replaced. |
| 3 | **DPDP Rules 2025 Gazette notification** | Commencement dates | PIB says notified 14 Nov 2025; earlier commentary said 13 Nov. Core data-fiduciary obligations reportedly bite ~18 months after notification. Treated as a design constraint regardless of the exact date. |
| 4 | **Maharashtra Land Revenue Code 1966** | Confirming s.282 as the Property Card basis | Property Card treated as a verification source on its practical merits; the statutory citation stays `SECONDARY_ONLY`. |
| 5 | **RERA commencement notification** | The s.3(2)(b) exemption date gate | Exemption for projects completed "prior to commencement" cannot be evaluated on a date basis. |
| 6 | **Maharashtra RERA rules/notification** | Whether the state reduced the s.3(2)(a) threshold | Threshold treated as unsettled; MahaRERA absence defaults to `NOT_APPLICABLE`. |

## Blocked on information only the business can supply

| # | Item | What it blocks |
|---|---|---|
| 7 | **Does the lender already hold a CERSAI entity account?** | Whether our single best automated verification adapter (T1/T2) is actually reachable. Faster to ask than to research. |
| 8 | **Document corpus for the Phase 0 survey** | Real OCR/extraction accuracy measurement. `tools/corpus_survey.py` is written and tested against synthetic fixtures; it needs real Mumbai documents to produce meaningful numbers. |
| 9 | **Are valuation reports part of the case bundle?** | Three verified requirements (`REQ_HFC_AXIV_1_2/1_5/1_6`) depend on them, but valuation reports are not among the five MVP document types. |
| 10 | **Item 3 of the scope message of 2026-08-24** | Unknown — the message was truncated and the user confirmed it was a mistake. Recorded only so it is not mistaken for an omission. |

## Blocked on environment

| # | Item | Detail |
|---|---|---|
| 11 | **Verify ADR-0006 from an Indian network** | MahaRERA and IGR Maharashtra reset the connection; RBI's PDF host and India Code return WAF/403. Cannot distinguish geo-restriction from bot filtering. Affects Phase 7 deployment topology. |
| 12 | **Read MahaRERA and IGR terms of use** | Needed to move either source off the provisional T4 (human-operated) tier. |

## Deferred by choice

| # | Item | Note |
|---|---|---|
| 13 | **Authentication / authorization** | Out of MVP scope per ADR-0002. Compensating controls recorded there: tenant/case scoping retained in the model, actor field retained in the ledger, API bound to private network. |
| 14 | **Temporal vs lightweight queue** | ADR open decision O2. Decide when the human-wait-state load in the verification layer is visible. |
| 15 | **PyMuPDF (AGPL) licensing** | ADR open decision O3. Raise with legal before adopting; `pypdfium2` used meanwhile. |
| 16 | **RBI Outsourcing of IT Services MD extraction** | File held. Confirm HFC applicability first — its addressee list says "NBFCs" *without* the "including HFCs" wording the other two RBI instruments use, so it cannot be inferred. |
| 17 | **SARFAESI s.23 filing time limit** | The commonly cited "30 days" is not in the text we hold; the provision carries amendment markers. Not encoded. |

---

## How these are enforced rather than remembered

`python tools/check_regulatory.py` fails or warns on anything that would let a blocked
requirement quietly become an active rule. Items 1 and 2 currently surface there as
warnings; item 6 surfaces as `REQUIRES_LEGAL_REVIEW`.
