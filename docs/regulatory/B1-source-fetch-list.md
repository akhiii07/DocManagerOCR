# B1 — primary sources: status and what is still needed

Updated 2026-08-24. See ADR-0006 for why these must be fetched manually.

**These are public regulatory documents with no privacy sensitivity.** Retrieval only.

Files live in `docs/regulatory/sources/`. Provenance is graded per file in
`sources.yaml` under `local_copy_provenance` — a PDF in that folder is not automatically
authoritative, and two of the current ones are not.

## Obtained and extracted

| File | Instrument | Status |
|---|---|---|
| `rbi-hfc-md-2021.pdf` | HFC Master Direction, 2021 | Para 19, Para 104, Annex XIV, Appendix XII(b) extracted |
| `transfer-of-property-act-1882.pdf` | Transfer of Property Act, 1882 | s.54 and Chapter IV (s.58–59) extracted |
| `sarfaesi-act-2002.pdf` | SARFAESI Act, 2002 | Chapter IV (s.22, 23, 26) and Chapter IVA (s.26C–26E) extracted |
| `rera-act-2016.pdf` | RERA Act, 2016 | s.3 extracted; corrected the B0 threshold error |

## Obtained, extraction pending

| File | Instrument | Next |
|---|---|---|
| `rbi-outsourcing-it-md-2023.pdf` | Outsourcing of IT Services MD | Binds *this platform* as an IT service. Confirm HFC applicability first — the addressee list says "NBFCs" without the "including HFCs" wording used elsewhere. |
| `mh-city-survey-rules-1969.pdf` | City Survey Rules, 1969 | Background on the Property Card mechanism. Aggregator copy — orientation only. |
| `dpdp-rules-2025-pib.pdf` | DPDP Rules press release | Explanatory only, not the operative text. |

## Still needed

| Save as | Why it matters | Priority |
|---|---|---|
| `registration-act-1908.pdf` (full, India Code) | The current copy is an **Indian Kanoon extract of s.17 only**, so its two requirements are recorded but **blocked**. The real gap is **s.49 — effect of non-registration**: without it the platform can detect a missing registration but cannot state its consequence, which is the part a Risk Manager acts on. | **1** |
| `maharashtra-stamp-act-1958.pdf` (current, India Code) | Current copy is a 2019 aggregator text that self-declares it could not be verified. **No stamp-duty requirement has been extracted and none may be** until this is replaced. A Fourth Amendment in 2026 confirms the Act is actively amended. | **2** |
| `dpdp-rules-2025-gazette.pdf` | To settle the notification date (PIB says 14 Nov 2025; earlier commentary said 13 Nov) and the commencement schedule. | 3 |
| `mlrc-1966.pdf` | Confirm the s.282 basis for the Property Card. | 4 |
| RERA commencement notification | s.1(3) leaves commencement to notification; the date gates the s.3(2)(b) exemption. | 4 |
| Maharashtra RERA rules / notification | Whether the state used the s.3(2)(a) proviso to reduce the registration threshold. | 4 |

## Rejected

- **A second Transfer of Property Act copy** (supplied 2026-08-24) — same aggregator as the
  stale stamp act, marked *"version of this document from 1 January 2003"* and *"could not
  be verified"*. The incumbent official bare-act copy was retained. Recorded in
  `sources.yaml` so the rejection is not silently reversed.

## Checking

```bash
python tools/check_regulatory.py
```

Enforces that a requirement may only become a rule if its source is `PRIMARY_VERIFIED` and
it is not flagged `REQUIRES_LEGAL_REVIEW`. Also fails if any PDF in `sources/` lacks a
provenance grade — an ungraded document is an unverified document.
