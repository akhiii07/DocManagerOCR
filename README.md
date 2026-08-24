# DocManagerOCR

Private collateral document intelligence, compliance verification and risk assessment
platform for lending underwriting.

> This is **not** an OCR tool, a PDF chatbot, or a document parser. OCR is the lowest
> layer. The product is an auditable decision-support system that tells a Risk Manager
> what a bundle of collateral documents establishes, what is inconsistent, what can be
> externally verified, which compliance checkpoints pass, and what needs human review.

## Status

**Phase 0 (Foundation & Research) + B0 (Authority Map) — in progress.**

No pipeline code yet. Current deliverables are the corpus survey tool, the regulatory
authority map, and the data-handling policy.

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
docs/
  decisions.md                    architecture decision record
  privacy/
    data-handling-policy.md       the hard constraint, stated operationally
  regulatory/
    authority-map.md              B0 deliverable — who governs what, human-readable
    sources.yaml                  B0 deliverable — machine-readable source register
tools/
  corpus_survey.py                Phase 0 — measure the real document corpus
  make_fixtures.py                synthetic fixtures (tier-1 dev data)
  requirements.txt
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r tools/requirements.txt
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
