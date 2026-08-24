# B0 — Authority Map

**Scope:** NBFC/HFC home loan + loan-against-property, collateral in Mumbai / Maharashtra.
**Compiled:** 2026-08-24
**Status:** B0 complete — this is a *map of who governs what and where the official text
lives*. It is **not** the requirement register. Extracting atomic requirements with exact
section numbers is B1.

---

## How to read the verification status column

| Status | Meaning |
|---|---|
| `PRIMARY_VERIFIED` | Retrieved and read from the authority's own site during this research pass. Identifiers and dates below are quoted from that source. |
| `PRIMARY_LOCATED` | Official source located and identified, but substantive text not yet read. Metadata may be from the source's index page. |
| `SECONDARY_ONLY` | Only secondary sources consulted. **Must not be used as the basis of any compliance rule** until confirmed against the primary source. |
| `UNREACHABLE` | Official source identified but could not be retrieved during this pass. See ADR-0006. |

**Rule:** no compliance rule enters the rule base from a `SECONDARY_ONLY` row.

---

## Layer 1 — Prudential and conduct regulator

### Reserve Bank of India (RBI)

Regulates HFCs and NBFCs. Since the transfer of HFC regulation from NHB, HFCs are regulated
by RBI, with NHB retaining supervision-related functions — **the precise current split of
regulatory vs supervisory responsibility is a B1 question**, and it matters because it
determines which body's instruments bind our lender.

| Instrument | Reference | Dates | Applicability | Status |
|---|---|---|---|---|
| Master Direction – Non-Banking Financial Company – Housing Finance Company (Reserve Bank) Directions, 2021 | RBI/2020-21/73<br>DOR.FIN.HFC.CC.No.120/03.10.136/2020-21 | Issued 2021-02-17; index page shows "updated as on 2025-07-17" | HFCs | `PRIMARY_LOCATED` |
| Responsible Lending Conduct – Release of Movable / Immovable Property Documents on Repayment / Settlement of Personal Loans | RBI/2023-24/60<br>DoR.MCS.REC.38/01.01.001/2023-24 | Issued 2023-09-13; **effective 2023-12-01** | Commercial banks, LABs, UCBs, StCBs/DCCBs, **all NBFCs including HFCs**, ARCs | `PRIMARY_VERIFIED` |
| Reserve Bank of India (Digital Lending) Directions, 2025 | RBI/2025-26/36<br>DOR.STR.REC.19/21.07.001/2025-26 | Issued 2025-05-08; immediate, except Para 6 from 2025-11-01 and Para 17 from 2025-06-15 | Commercial banks, co-op banks, **NBFCs including HFCs**, AIFIs | `PRIMARY_VERIFIED` |
| Master Direction on Outsourcing of Information Technology Services | id=12486 on rbi.org.in | 2023 | Includes NBFCs. **HFC applicability not yet confirmed** | `SECONDARY_ONLY` |
| Master Direction – NBFC Scale Based Regulation | To locate | — | NBFCs; HFC interaction to confirm | `PRIMARY_LOCATED` |
| Directions on Managing Risks and Code of Conduct in Outsourcing of Financial Services | To locate | — | To confirm | `PRIMARY_LOCATED` |
| Master Direction – KYC | To locate | — | To confirm | `PRIMARY_LOCATED` |

**Verified detail worth carrying into B1 — the document-release obligation.**
This is the clearest example in the whole corpus of a regulatory requirement that converts
cleanly into a deterministic, machine-checkable rule:

- Release **all** original movable/immovable property documents and remove registered
  charges **within 30 days** of full repayment/settlement.
- Delay beyond that: compensate the borrower **₹5,000 per day of delay**.
- Where documents are lost/damaged, the RE must assist in obtaining duplicates and bear the
  cost, with an additional 30-day grace period.

Note what this does and does not give us. It is a **post-closure obligation**, so it does
not gate origination underwriting — but it does impose a hard requirement that the lender
know, per case, exactly which original documents it holds. That makes *document custody
inventory* a first-class output of our platform, not an afterthought. Flagging this because
it is a genuine scope implication that was not in the original brief.

**Relevance to us — three distinct roles, which B1 must keep separate:**
1. *Rules the platform checks on a case* (collateral documentation, LTV, valuation).
2. *Rules the platform itself must satisfy as a system* (data storage location, consent,
   audit trails, outsourcing/third-party arrangements). The Digital Lending Directions'
   data-localisation and consent provisions bind **our architecture**, not the borrower's
   documents.
3. *Rules that generate obligations the platform should track* (document release on closure).

---

## Layer 2 — Central statutes governing property and security

| Instrument | Authority | Relevance | Official source | Status |
|---|---|---|---|---|
| Registration Act, 1908 — esp. s.17 (documents whose registration is compulsory) | Parliament / Dept of Land Resources | Determines whether a given deed *must* be registered; an unregistered instrument that s.17 requires to be registered is a document defect, not a formatting issue | India Code | `PRIMARY_LOCATED` |
| Transfer of Property Act, 1882 | Parliament | Governs sale, mortgage, charge, and what constitutes valid transfer | India Code | `PRIMARY_LOCATED` |
| Maharashtra Stamp Act, 1958 (text as on 2025-04-08) | Maharashtra | Stamp duty adequacy on the instrument; under-stamping is a defect affecting admissibility | India Code | `PRIMARY_LOCATED` |
| SARFAESI Act, 2002 — Chapter IV | Parliament | Statutory basis for the central security-interest registry | India Code | `PRIMARY_LOCATED` |
| Real Estate (Regulation and Development) Act, 2016 | Parliament | Basis for RERA registration obligations; drives what MahaRERA holds | India Code | `PRIMARY_LOCATED` |
| Maharashtra Land Revenue Code, 1966 | Maharashtra | Reported basis for the urban Property Card (City Survey record). Section reference cited by secondary sources as s.282 — **unconfirmed** | India Code | `SECONDARY_ONLY` |

---

## Layer 3 — Real estate regulator

### MahaRERA — Maharashtra Real Estate Regulatory Authority

- Official portal: `https://maharera.maharashtra.gov.in/`
- Public search reported by secondary sources on: project name, promoter name, district,
  MahaRERA registration number, promoter PAN.
- **Status: `UNREACHABLE` from this research environment (connection reset).** See ADR-0006.

**What we would verify against it:** project name, promoter/developer identity, registration
number, project status, registered address, phase, registration validity, completion
information — compared against the Agreement of Sale and Sale Deed.

**Access tier: not yet assignable.** Assigning a tier requires reading MahaRERA's actual
terms of use, which could not be retrieved. **Provisional working assumption: T4
(portal, human-operated).** Do not build an automated adapter against it until the terms are
read from an Indian network.

**Applicability caveat that must be encoded, not assumed.** RERA registration applies to
projects meeting statutory thresholds (secondary sources report land area >500 sq m or >8
apartments — to confirm in B1). A large share of Mumbai LAP collateral will be older resale
flats and independent properties with **no RERA record at all**. For those, the correct
verification result is `NOT_APPLICABLE`, not `NOT_FOUND_IN_SOURCE`, and certainly not a
finding. Getting this distinction wrong would generate false positives on a large fraction
of real cases.

---

## Layer 4 — State registration and land records

### Department of Registration & Stamps, Maharashtra (IGR)

- Official portal: `https://igrmaharashtra.gov.in/`
- Services reported: property registration, **eSearch** of registered documents, Index II
  retrieval, valuation / ready reckoner, stamp duty computation.
- **Status: `UNREACHABLE` from this research environment (connection reset).**

**Why this is the single highest-value external source for us.** Index II is the registration
summary for a registered instrument — it independently attests the parties, the property, the
consideration and the registration particulars. Comparing a Sale Deed against its own Index II
is close to a direct authenticity check, and it is the strongest signal available against
forged or altered deeds.

**Access tier: provisionally T4.** Secondary sources describe the free eSearch as
CAPTCHA-gated but not login-gated. CAPTCHA-gated means human-operated by our own rule — the
brief explicitly excludes bypassing bot detection.

### Maharashtra Land Records / Bhumi Abhilekh — City Survey Office

- Portal: `https://bhumiabhilekh.maharashtra.gov.in/`
- **Mumbai-specific and important:** the relevant urban land record is the **Property Card
  (PR Card / Malmatta Patrak)** maintained by the City Survey Office, keyed by **CTS number**
  — *not* the 7/12 extract used for rural land. Mumbai Suburban District is reported to have
  ten City Survey Offices covering 86 villages.
- Status: `SECONDARY_ONLY`.

**This has a direct architectural consequence.** Our property identity model cannot assume
"survey number" is the universal parcel key. For Mumbai the key is **CTS number**; elsewhere
in Maharashtra it may be survey number / gat number / hissa number. The canonical property
model needs a **parcel-identifier type** field, not a single `survey_number` string. Encoding
the Mumbai case as "survey number" would break the moment we add a second district.

---

## Layer 5 — Municipal

### Municipal Corporation of Greater Mumbai (MCGM / BMC) — property tax

- Portal: `https://ptaxportal.mcgm.gov.in/CitizenPortal/`
- Keyed by **Property Account Number (P-ID) / Assessment Number**; CAPTCHA on entry.
- Search-by-ward/name/address reported as available where the account number is unknown.
- Status: `SECONDARY_ONLY`. **Access tier: provisionally T4.**

**What we verify:** assessee name, property description/address, assessment number,
outstanding dues, payment history — against the Property Tax documents and the deed.
Outstanding municipal dues are a genuine collateral risk indicator, not merely a data point.

---

## Layer 6 — Central security-interest registry

### CERSAI

- Central Registry of Securitisation Asset Reconstruction and Security Interest of India.
- Operates under Chapter IV of the SARFAESI Act, 2002; operational since 2011-03-31.
- Purpose: prevent multiple lending against the same collateral by registering security
  interests, including mortgages over immovable property and units under construction.
- Access is via registered institutional entity accounts (banks/FIs), not anonymous public
  lookup.
- Status: `SECONDARY_ONLY` on the access mechanics; the statutory basis is `PRIMARY_LOCATED`.

**Assessment: this is the most promising T1/T2 candidate in the entire map**, and probably
the highest-value automated check we can build. It is central rather than state-fragmented,
it is institutionally accessed rather than CAPTCHA-gated, and an existing charge against the
same property is one of the most material collateral findings possible. **Priority for B1
and for Phase 7.**

Open question for B1: what programmatic access CERSAI offers to a registered entity, under
what agreement. Since our lender is an NBFC/HFC it plausibly already has a CERSAI entity
account — worth asking the business directly rather than researching from outside.

---

## Layer 7 — Data protection

### MeitY — Digital Personal Data Protection

| Instrument | Dates | Status |
|---|---|---|
| Digital Personal Data Protection Act, 2023 | Enacted 2023 | `PRIMARY_LOCATED` |
| Digital Personal Data Protection Rules, 2025 | Reported notified **2025-11-13**, phased: Board provisions immediate; Consent Manager obligations at +12 months; **core obligations on data fiduciaries (notice, breach reporting, compliance) at +18 months ≈ 2027-05-13** | `SECONDARY_ONLY` |

The PIB document retrieved during this pass did not cleanly confirm the phasing. **The dates
above come from secondary legal commentary and must be confirmed against the Gazette
notification in B1** before any of them is treated as a deadline.

If the +18-month figure holds, core data-fiduciary obligations land roughly nine months from
now. That is squarely inside this platform's likely production timeline, so DPDP requirements
should be treated as **design constraints now**, not as a later compliance retrofit.

---

## Consolidated view: what this map means for the build

**1. Regulatory obligations split into three kinds, and B1 must tag every requirement.**
Rules the platform *checks*; rules the platform *must itself satisfy*; obligations the
platform *tracks*. Conflating them produces a rule base that cannot be executed.

**2. Automated external verification will be thin at MVP.** Of six candidate sources, exactly
one (CERSAI) currently looks like a plausible T1/T2 automated adapter. MahaRERA, IGR eSearch,
and MCGM property tax are all provisionally T4 human-operated. The T4 task queue is therefore
not a nice-to-have — **it is the primary delivery mechanism for external verification in the
MVP**, and Phase 7 should be planned around it.

**3. Two architectural corrections fall out of this research:**
   - Parcel identity must be typed (`CTS number` for Mumbai, not `survey_number`).
   - Document custody inventory becomes an output, driven by the 30-day release obligation.

**4. Three things must be verified from an Indian network before Phase 7 design:**
   - MahaRERA terms of use and search behaviour
   - IGR eSearch terms of use and CAPTCHA behaviour
   - Whether ADR-0006 (geo-restriction) is real

**5. Nothing here is a rule yet.** No item in this map has been converted into a checkpoint,
and none should be until B1 extracts atomic requirements with exact section references from
primary sources, and legal signs off.

---

## Sources consulted

Primary:
- [RBI — Responsible Lending Conduct: Release of Movable/Immovable Property Documents](https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12535&Mode=0)
- [RBI — Reserve Bank of India (Digital Lending) Directions, 2025](https://rbi.org.in/Scripts/NotificationUser.aspx?Id=12848&Mode=0)
- [RBI — HFC Master Direction (index)](https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12030)
- [RBI — Master Direction on Outsourcing of IT Services (index)](https://www.rbi.org.in/scripts/BS_ViewMasDirections.aspx?id=12486)
- [India Code — The Registration Act, 1908](https://www.indiacode.nic.in/bitstream/123456789/15937/1/the_registration_act,1908.pdf)
- [India Code — Registration Act s.17](https://www.indiacode.nic.in/show-data?actid=AC_CEN_18_43_00004_190816_1523340837338&orderno=18)
- [India Code — The Maharashtra Stamp Act, 1958](https://www.indiacode.nic.in/bitstream/123456789/22026/1/the_maharashtra_stamp_act,_1958.pdf)
- [PIB — DPDP Rules, 2025 Notified](https://static.pib.gov.in/WriteReadData/specificdocs/documents/2025/nov/doc20251117695301.pdf)

Official portals identified (not retrievable this pass):
- [MahaRERA](https://maharera.maharashtra.gov.in/) · [IGR Maharashtra](https://igrmaharashtra.gov.in/) · [Maharashtra Bhumi Abhilekh](https://bhumiabhilekh.maharashtra.gov.in/) · [MCGM Property Tax](https://ptaxportal.mcgm.gov.in/CitizenPortal/) · [CERSAI](https://www.cersai.org.in/CERSAI/JSP/index.jsp)

Secondary (orientation only — never a rule basis):
- [Mumbai Suburban District — Land Records](https://mumbaisuburban.gov.in/en/land-records/)
- [Wikipedia — CERSAI](https://en.wikipedia.org/wiki/Central_Registry_of_Securitisation_Asset_Reconstruction_and_Security_Interest)
- [Wikipedia — Digital Personal Data Protection Rules, 2025](https://en.wikipedia.org/wiki/Digital_Personal_Data_Protection_Rules,_2025)
