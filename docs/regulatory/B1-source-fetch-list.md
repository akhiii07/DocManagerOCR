# B1 — primary sources to fetch locally

B1 (requirement extraction) is blocked on document access, not on analysis. See ADR-0006:
every PDF-hosted primary instrument is unreachable from the research environment, while
search engines can still see them.

**These are public regulatory documents. There is no privacy sensitivity here** — this is
purely a retrieval problem. Download them and B1 proceeds immediately.

## Where to put them

```
docs/regulatory/sources/
```

Gitignored — they are large and not ours to redistribute. Keep the filenames below so
citations in the requirement register resolve.

## Priority 1 — needed to start B1

| Save as | Document | Where |
|---|---|---|
| `rbi-hfc-md-2021.pdf` | Master Direction – NBFC-HFC (Reserve Bank) Directions, 2021 (RBI/2020-21/73) | [RBI Master Direction page](https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12030) → follow the PDF link |
| `registration-act-1908.pdf` | The Registration Act, 1908 — s.17 is the target | [India Code](https://www.indiacode.nic.in/bitstream/123456789/15937/1/the_registration_act,1908.pdf) |
| `maharashtra-stamp-act-1958.pdf` | The Maharashtra Stamp Act, 1958 (text as on 2025-04-08) | [India Code](https://www.indiacode.nic.in/bitstream/123456789/22026/1/the_maharashtra_stamp_act,_1958.pdf) |

## Priority 2 — needed to complete B1

| Save as | Document | Where |
|---|---|---|
| `transfer-of-property-act-1882.pdf` | The Transfer of Property Act, 1882 | India Code |
| `sarfaesi-act-2002.pdf` | SARFAESI Act, 2002 — Chapter IV (CERSAI basis) | India Code |
| `rera-act-2016.pdf` | Real Estate (Regulation and Development) Act, 2016 — registration threshold | India Code |
| `rbi-outsourcing-it-md-2023.pdf` | Master Direction on Outsourcing of IT Services | [RBI](https://www.rbi.org.in/scripts/BS_ViewMasDirections.aspx?id=12486) |
| `mlrc-1966.pdf` | Maharashtra Land Revenue Code, 1966 — confirm the Property Card section | India Code |
| `dpdp-rules-2025.pdf` | DPDP Rules, 2025 — **confirm commencement dates against the Gazette** | MeitY / eGazette |

## Already read — no download needed

- [RBI/2023-24/60 — Release of Property Documents](https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12535&Mode=0) — `PRIMARY_VERIFIED`
- [RBI Digital Lending Directions, 2025](https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12848&Mode=0) — `PRIMARY_VERIFIED`

## What B1 produces once unblocked

For each instrument: atomic requirements quoted verbatim with exact section references, an
applicability predicate, a feasibility class (deterministic / external-verifiable /
retrieval-assisted / LLM-assisted / human-only / out-of-scope), and an obligation kind
(platform checks / platform must satisfy / platform tracks).

Output: `docs/regulatory/requirements.yaml`, every entry citing a `sources.yaml` id whose
`verification_status` is `PRIMARY_VERIFIED`.

**Rules are not authored in B1.** That is B2, and no rule is enabled without legal sign-off.
