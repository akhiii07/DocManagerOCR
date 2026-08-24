# DocManagerOCR

Private collateral document intelligence, compliance verification and risk assessment
platform for lending underwriting.

> This is **not** an OCR tool, a PDF chatbot, or a document parser. OCR is the lowest
> layer. The product is an auditable decision-support system that tells a Risk Manager
> what a bundle of collateral documents establishes, what is inconsistent, what can be
> externally verified, which compliance checkpoints pass, and what needs human review.

## Status

| Track | State |
|---|---|
| **P0** Foundation | Corpus survey tool built and tested. Awaiting real documents. |
| **B0** Authority map | Done — 8 authorities, 12 instruments mapped. |
| **B1** Requirement extraction | 21 requirements, 18 rule-ready, 3 blocked. |
| **P5** Canonical data model | Done — `src/dmocr/model/`, 49 tests passing. |
| Next | Findings + rule engine |

Deferred items are tracked in [docs/OPEN-ITEMS.md](docs/OPEN-ITEMS.md).

## MVP scope

| Dimension | Decision |
|---|---|
| Lender type | NBFC / HFC — home loan + loan-against-property |
| Jurisdiction | Mumbai / Maharashtra only |
| Document types | Agreement of Sale, Sale Deed, Property Papers, Property Tax, Possession Documents |
| Compute | 1–2 GPUs, 48–80 GB each |
| Test data | Real documents, local machine only |
| Auth/AuthZ | **Out of scope for MVP** (see ADR-0002) |

## Hard constraint

Customer document content must never reach an external AI provider — in production or
during development. See [docs/privacy/data-handling-policy.md](docs/privacy/data-handling-policy.md).

## Layout

```
src/dmocr/model/                  canonical data model (claims, not fields)
tests/                            49 tests
docs/
  decisions.md                    architecture decision record
  canonical-model.md              why the model has this shape
  OPEN-ITEMS.md                   everything deferred, in one place
  privacy/
    data-handling-policy.md       the hard constraint, stated operationally
  regulatory/
    authority-map.md              B0 deliverable — who governs what, human-readable
    sources.yaml                  B0 deliverable — machine-readable source register
  regulatory/
    authority-map.md              B0 — who governs what, human-readable
    sources.yaml                  B0 — machine-readable source register + provenance
    requirements.yaml             B1 — atomic requirements, quoted with citations
    B1-source-fetch-list.md       which primary instruments are still needed
    sources/                      the primary instruments themselves (public)
tools/
  corpus_survey.py                Phase 0 — measure the real document corpus
  make_fixtures.py                synthetic fixtures (tier-1 dev data)
  reg_text.py                     B1 — search primary instruments
  check_regulatory.py             B1 — enforce knowledge-base invariants
  requirements.txt
```

## The invariant

A requirement may only be promoted to an executable rule if its source is
`PRIMARY_VERIFIED` — read from an authoritative copy — and is not flagged
`REQUIRES_LEGAL_REVIEW`. This is enforced, not just documented:

```bash
python tools/check_regulatory.py
```

Exit 0 clean, 1 error, 2 warnings. Warnings name every requirement that is *not* yet
rule-ready and why.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[tools,dev]"
```

```bash
python -m pytest tests -q
```

## Running the corpus survey

The survey reads documents locally, makes **no network calls**, and writes only
aggregate metrics — never document text. File names are hashed unless `--show-names`.

```bash
python tools/corpus_survey.py "D:/path/to/documents" --out survey-output
```

It answers the questions that decide the OCR strategy before any of it is built: how many
documents already carry a usable text layer (never OCR what you can read), scan resolution
and sharpness distribution, page-count variance, and script mix.

## Validating the tooling without real data

```bash
python tools/make_fixtures.py fixtures
python tools/corpus_survey.py fixtures --out survey-output --show-names
```

Verified to exercise all three classification paths: `DIGITAL`, `MIXED`, `SCANNED`.

## Next steps

- **P0**: run the corpus survey against the real Mumbai/Maharashtra document set
  *(blocked — corpus not yet available)*
- **B1**: requirement extraction from the instruments in `docs/regulatory/sources.yaml`,
  starting with the HFC Master Direction *(unblocked)*
