# Ingestion and quality gate

`src/dmocr/ingest/` · tests in `tests/test_ingest.py`

```
bytes
  -> safety scan          refuse active content BEFORE any parser sees it
  -> content store        SHA-256 addressed; dedupe falls out of this
  -> structural analysis  text layer, dpi, sharpness, rotation, script mix
  -> quality gate         OK | DEGRADED | REJECTED
  -> Document on the Case
```

## Why this order

**Safety scan runs on raw bytes before parsing**, because the parser is the thing being
protected.

**Storage happens before analysis**, so a file that crashes the parser is still retained
for a human to look at. Losing evidence because we could not read it would be the wrong
failure.

**Classification and extraction are not here.** Ingestion establishes that we have a
readable artefact and what condition it is in. Deciding what the document *is* has its own
failure modes and belongs in its own stage.

## The verdict that matters is DEGRADED

Real collateral bundles are often poor quality. Rejecting outright pushes work back to a
human with no explanation, so `REJECTED` is reserved for documents that genuinely cannot
be processed: encrypted, unparseable, zero pages, over the page limit.

Everything else that is wrong — low resolution, blur, partial text layer, rotation, mixed
page sizes — produces **`DEGRADED`: process, but cap confidence.** The document flows
through the pipeline while every claim extracted from it carries a ceiling.

Note in particular that a **very low resolution scan is DEGRADED, not REJECTED**. A poor
scan is still evidence a reviewer may want to see; refusing it removes that option.

And a **rejected document is still attached to the case**. The reviewer needs to see that
a file arrived and why it was unusable, rather than finding a silent gap in the bundle.

## Safety scanning, and its honest limitation

`sanitize.scan()` checks magic bytes and the declared capabilities of a PDF.

| Blocked | Suspicious (processed with a note) |
|---|---|
| `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/GoToR`, `/SubmitForm` | `/OpenAction`, `/AA`, `/XFA`, truncation, extension mismatch |

Blocked content is **never stored** — persisting active content "just in case" would put
the risky bytes in our object store.

Format is determined by **magic bytes, not the filename**. A filename is an assertion by
the uploader; the bytes are the fact.

**Stated limitation:** this scans raw bytes for capability names. A PDF can hide object
definitions inside compressed object streams where those names do not appear in plaintext,
so a determined adversary can evade it. It is a cheap first filter, **not a security
boundary**. The real boundaries are rendering in a sandboxed process with no network
egress, and never executing what a document declares — deployment concerns, recorded in
the data-handling policy.

## Thresholds are data, not constants

`QualityThresholds` is a Pydantic model so limits can be adjusted per tenant or document
type without a release, and so changes appear in configuration review rather than buried
in a diff.

| Threshold | Default | Note |
|---|---|---|
| `min_dpi` | 200 | Below this, OCR accuracy degrades on Indian legal documents |
| `reject_dpi` | 100 | Below this, OCR output should not be relied on |
| `min_sharpness` | 60 | **Placeholder.** Corpus-relative — re-tune from the survey p10 |
| `max_pages` | 800 | |

`min_sharpness` deserves the warning. Laplacian variance is only comparable between pages
rendered at the same scale, so the default is a guess until `tools/corpus_survey.py` has
run against real documents. On the synthetic fixtures a good scan measures ~8200 and a
blurred one ~8 — the separation is enormous, which is exactly why a real-corpus number is
needed rather than a made-up midpoint.

## Single source of truth for measurement

`tools/corpus_survey.py` and the production quality gate both use
`dmocr.ingest.pdfinfo`. This is deliberate: a threshold tuned against survey numbers has
to mean the same thing at ingest time, and two copies of the analysis would drift.

## Content addressing

Documents are stored under the SHA-256 of their bytes. Deduplication falls out for free —
a renamed copy is still a duplicate — and, more importantly, every piece of evidence ties
to exact content. A finding citing page 4 of `DOC123` refers to an immutable byte
sequence, not to whatever currently sits at a mutable path.

`LocalContentStore` writes to a `.partial` name and renames, so a crash cannot leave a
truncated blob visible under a hash that promises complete content. It provides **no
encryption at rest** and is development-only; its directory must live outside the
repository.

## Not yet built

- Encryption at rest and per-tenant keys (deployment).
- Virus scanning (an external service; the interface has a place for it).
- Page-level routing for `MIXED` documents — currently flagged, not yet acted on.
- Async job handling. Ingestion is synchronous; the 202-plus-status pattern arrives with
  the API layer.
