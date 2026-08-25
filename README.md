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
| **P5** Canonical data model | Done — `src/dmocr/model/`. |
| **P6** Rule engine + findings | Done — `src/dmocr/rules/`, 12 rules, all `DRAFT`. |
| **P1** Ingestion + quality gate | Done — `src/dmocr/ingest/`. |
| **P2** OCR + text layer | Done — `src/dmocr/ocr/`, per-page routing, RapidOCR. |
| **P3** Classification | Done — `src/dmocr/classify/`, rule-based baseline. |
| **P4** Structured extraction | Done — `src/dmocr/extract/`, deterministic + grounding. |
| **P7** Cross-document validation | Done — `src/dmocr/resolve/` + `pipeline.py`. |
| **P8** Verification orchestrator | Done — `src/dmocr/verify/`, planner + T4 task queue. |
| **P9** Verification-aware rules | Done — external results become findings. |
| **P10** Evaluation harness | Done — `src/dmocr/eval/`, metrics that don't reward guessing. |
| **P12** Review UI | Done — `src/dmocr/web/`, upload boxes + findings + evidence crops. |
| Tests | 523 passing. |
| Next | Per-field accept/correct, operator task list |

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
src/dmocr/rules/                  rule engine (policy in YAML, computation in Python)
src/dmocr/ingest/                 upload → safety scan → store → quality gate
src/dmocr/ocr/                    text layer + OCR, routed per page
src/dmocr/classify/               which extraction schema applies
src/dmocr/extract/                schemas, finders, span grounding -> claims
src/dmocr/resolve/                entity resolution + case assembly
src/dmocr/verify/                 source planning, adapters, operator tasks, comparison
src/dmocr/eval/                   ground truth, metrics, regression gates
src/dmocr/web/                    review UI (FastAPI + Jinja, no build step)
src/dmocr/pipeline.py             ingest -> ocr -> classify -> extract -> assemble -> rules
rules/mvp.yaml                    the rule set — all DRAFT until legal sign-off
tests/
docs/
  decisions.md                    architecture decision record
  canonical-model.md              why the model has this shape
  rule-engine.md                  how rules are authored, gated and evaluated
  ingestion.md                    the quality gate and what it deliberately does not do
  ocr.md                          per-page routing, coordinates, and what is unmeasured
  classification.md               the cross-reference problem, and why UNKNOWN is a feature
  extraction.md                   grounding, Indian conventions, what real OCR taught us
  cross-document.md               entity resolution, and why false merging is the worse error
  verification.md                 tiers, data minimisation, and why unavailable is not failure
  evaluation.md                   why the metrics must not reward guessing
  ui.md                           the box check, and where gating is right
  OPEN-ITEMS.md                   everything deferred, in one place
  privacy/
    data-handling-policy.md       the hard constraint, stated operationally
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
  evaluate.py                     run the evaluation harness
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
.venv/Scripts/python.exe -m pip install -e ".[tools,dev,ocr,web]"
```

```bash
python -m pytest tests -q
```

## Review UI

```bash
python -m dmocr.web
```

Localhost by default. Upload boxes per document type, a three-outcome type check,
case-level findings, and click-a-field-to-see-the-region-on-the-page.

To share it through a tunnel:

```bash
python -m dmocr.web --public                      # generates an access token
cloudflared tunnel --url http://127.0.0.1:8000    # another terminal
```

Non-loopback binding is refused **unless a token is configured** — the conditional form of
the ADR-0002 control: localhost, or authenticated, never neither. Public mode shows a
demo banner, because a single shared token gives no per-user identity and no revocation.
**Synthetic fixtures only** — real documents need real auth and a private deployment.

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

Fixtures cover every text-layer path (`DIGITAL`, `MIXED`, `SCANNED`), a good and a poor
scan, a phone photo, and a page of rendered text for exercising OCR end to end.

## The binding constraint

Everything above is tested against **synthetic fixtures**. That is enough to prove the
plumbing — routing, coordinates, caching, provenance, rule gating — but it says nothing
about accuracy on real Mumbai documents.

The evaluation harness now exists and runs, but on **synthetic fixtures its results
measure plumbing, not competence** — the values are the ones the generator wrote. Real
accuracy numbers still require the real corpus. See items 8, 22, 23, 24 and 28 in
[docs/OPEN-ITEMS.md](docs/OPEN-ITEMS.md).

The one genuinely informative measurement so far is OCR on a rendered page: **CER 4.2%,
WER 29.8%** — characters read well, word boundaries do not.

Running one real recogniser over one generated page already found four pattern bugs — OCR
drops word boundaries, and every whitespace-dependent pattern failed on it. That is the
clearest available evidence that extraction quality cannot be assessed on synthetic data.
See [docs/extraction.md](docs/extraction.md).
