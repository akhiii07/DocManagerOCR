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
| 18 | **Sandboxed rendering + egress control for uploads** | ADR-0012. The byte-level safety scan is a filter, not a security boundary. Real protection is process isolation with no network egress, plus virus scanning. Required before production, not before more building. |
| 19 | **Re-tune `QualityThresholds.min_sharpness`** | Default of 60 is a placeholder. Laplacian variance is corpus-relative, so it needs the p10 from a real corpus survey. Depends on item 8. |
| ~~20~~ | ~~Page-level routing for `MIXED` documents~~ | **Closed 2026-08-24** by `dmocr.ocr.service` — routing is per page, tested on `mixed_bundle.pdf`. |
| 21 | **Validate the Marathi classification lexicon** | `src/dmocr/classify/signals.py` carries Devanagari terms (खरेदीखत, करारनामा, मालमत्ता कर, ताबा, गहाण) at deliberately low weights, so they corroborate but cannot decide a classification alone. **Not checked against real Maharashtra instruments by a Marathi reader.** Needs review before weights go up. Disable with `ClassifierConfig(use_devanagari=False)`. |
| 22 | **No classification accuracy numbers** | Tests pin behaviour, not accuracy — there is no labelled corpus to measure against. Depends on item 8. |
| 23 | **No OCR accuracy measurement (CER/WER)** | RapidOCR reads a cleanly *rendered* fixture at ~0.88 confidence. That is the easiest possible input — no scanner noise, skew, stamps or handwriting. Real accuracy needs ground truth from the corpus. Depends on item 8. |
| 24 | **Devanagari OCR untested** | The bundled PP-OCR models claim multi-script support, but no Marathi document has been through this pipeline. Relates to item 21. |
| 25 | **Reading order is crude** | Blocks sort top-to-bottom then left-to-right, which is wrong for multi-column layouts. Needs layout analysis, not a tweak to the sorter. |
| 26 | **OCR cache holds customer text** | `FileOcrCache` entries contain extracted document text. The cache directory must sit outside the repo, out of off-machine backups, and under the retention policy. Same class of control as the content store. |
| 28 | **No extraction accuracy numbers (precision/recall per field)** | Deterministic finders are tested for behaviour, not accuracy. Real numbers need a labelled corpus. Depends on item 8. |
| 29 | **Model-based extraction not built** | Semantic fields — title chain recitals, restrictive covenants, encumbrance narratives — need a VLM/LLM extractor. It slots in behind the same `FieldFinder` interface and is subject to the same span-grounding control. Needs GPU (item 3 of scope). |
| ~~30~~ | ~~Entity resolution not built~~ | **Partly closed 2026-08-24** by `dmocr.resolve.names` — name matching with honorifics, initials, transliteration folds, patronymics and OCR-glued tokens. Address matching remains open (item 32). |
| 32 | **Address matching not built** | Only typed parcel identifiers are compared across documents. Unstructured Mumbai addresses need their own component and adversarial evaluation set. |
| 33 | **Name-match thresholds are uncalibrated** | `MATCH_THRESHOLD` 0.92 and `MISMATCH_THRESHOLD` 0.75 are starting points. They need tuning against reviewer outcomes on real documents; scores are reported so tuning is possible. Depends on item 8. |
| 35 | **No real CERSAI adapter** | The one plausible automated source, with the strongest legal footing (SARFAESI s.26). Blocked on item 7 (does the lender hold an entity account?) and ADR-0006. The orchestrator, planner and comparison are built and tested against a static adapter. |
| ~~36~~ | ~~No rule consumes verification results~~ | **Closed 2026-08-24.** `external_agreement`, `external_record_presence` and `verification_coverage` predicates; rules `EXT_CERSAI_CHARGE_001`, `EXT_OWNER_MATCH_001`, `EXT_AREA_MATCH_001`, `EXT_COVERAGE_001`. A prior charge is a CRITICAL BLOCKER citing `REQ_SARFAESI_26C`. |
| 37 | **Freshness windows are placeholders** | `DEFAULT_FRESHNESS` in `verify/sources.py` guesses 30/90/180/365 days per source. Not researched against how often each authority's records actually change. |
| 38 | **No artefact storage for operator captures** | `Snapshot.artefact_ref` is a reference only; the captured page/PDF is not yet stored or retained under policy. Same class of control as the content store. |
| 34 | **Title chain validation not built** | Whether the seller in document N is the buyer in document N-1 — the sequencing check that makes ownership continuity provable. Needs date-ordering checks across the transaction lifecycle too. |
| 31 | **OCR word-boundary loss degrades party extraction** | Real OCR returned `BETWEENRameshPatil` glued together. Patterns were made whitespace-tolerant, but a name whose internal space is lost extracts as `rameshpatil`. Needs a word splitter or a model extractor; not worth contorting regexes for. |
| 27 | **CPU-only OCR** | onnxruntime here has no GPU provider (~4 s/page). GPU needs a different onnxruntime build. Fine for pilot volumes; revisit against the throughput target. |
| 17 | **SARFAESI s.23 filing time limit** | The commonly cited "30 days" is not in the text we hold; the provision carries amendment markers. Not encoded. |

---

## How these are enforced rather than remembered

`python tools/check_regulatory.py` fails or warns on anything that would let a blocked
requirement quietly become an active rule. Items 1 and 2 currently surface there as
warnings; item 6 surfaces as `REQUIRES_LEGAL_REVIEW`.
