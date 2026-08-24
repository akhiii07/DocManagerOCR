# Text extraction (text layer + OCR)

`src/dmocr/ocr/` · tests in `tests/test_ocr.py`

## Per-page routing

The routing decision is made **per page, not per document**:

```
for each page:
    text layer has usable text?  -> extract exactly (no recognition error)
    otherwise, engine available? -> render at target dpi, recognise
    otherwise                    -> EMPTY, with the reason recorded
```

A bundle where a scanned annexure has been appended to a digitally generated deed is
ordinary. Treating the whole file one way either wastes accuracy on the digital pages or
wastes compute on them. The `mixed_bundle.pdf` fixture is exactly this shape, and routes
2 pages to the text layer and 1 to OCR.

This closes the `MIXED` case the quality gate flags.

## Coordinates: one system, converted at the edges

Everything is normalised to **top-left origin, PDF points**. Neither source uses that
natively:

| Source | Native | Conversion |
|---|---|---|
| PDFium text rects | `(left, bottom, right, top)`, **bottom-left** origin, points | y flipped against page height |
| OCR | **top-left pixels** at render scale | divided by scale |

Getting this wrong doesn't crash anything. It silently highlights the wrong region when a
reviewer clicks a finding — which destroys trust in every citation the system produces.
Four tests pin it.

## Confidence is `None` for text-layer blocks, not `1.0`

No recognition step happened. "No recognition occurred" and "recognition was perfectly
confident" are different claims, and the second is one we can't support. A malformed OCR
score likewise becomes `None`, never `0.0` or `1.0` — both would be assertions.

## Character offsets

Each block records its span in the assembled page text, so a `DocumentProvenance` can
carry a `TextSpan` *and* a `BoundingBox` for the same value. `block_at(offset)` maps an
offset back to a box; `blocks_for_span()` covers multi-line values.

This is what the span-grounding verifier (ADR-0004) will check against: a value the model
cannot locate in this text is discarded rather than reported.

## Reading order is crude, and that's recorded

Blocks are sorted top-to-bottom then left-to-right. That is **wrong for multi-column
layouts**. Proper reading-order analysis belongs with layout detection, not here. Stated
rather than pretended away.

## Caching

Keyed on `(content hash, page, engine id, dpi)`. OCR is the most expensive step and it's
deterministic for that tuple, so reprocessing a case becomes cheap — which matters because
reproducibility requires reprocessing to be routine.

The engine id includes its version, so upgrading the recogniser invalidates prior results
rather than serving output from a different model.

> **Privacy:** a cache entry contains extracted document text. It is customer content and
> is subject to the same rules as the documents — outside the repository, out of any
> off-machine backup, covered by retention. See the data-handling policy.

## Degrading honestly

| Situation | Behaviour |
|---|---|
| No engine installed | Digital PDFs still extract; scanned pages become `EMPTY` with a reason |
| One page fails OCR | That page is `EMPTY`, the rest still process, failure recorded in stats |
| Unparseable PDF | Reported in `stats.failures`, not raised |
| Corrupt cache entry | Discarded and recomputed |

A page we cannot read must still **appear** in the document, so the gap is visible rather
than silent.

## A bug worth remembering

`self.cache = cache or NullOcrCache()` silently discarded a freshly constructed cache,
because caches define `__len__` and an **empty cache is falsy**. Caching appeared to be
configured while never storing anything — a pure performance bug with no visible symptom
beyond slowness.

Fixed by testing `is None`, and `InMemoryOcrCache.__bool__` now returns `True` so the trap
can't recur. There is a regression test named for it.

## Engine choice

See ADR-0013. Briefly: **paddlepaddle publishes no wheels for Python 3.14**, so PaddleOCR —
the reference implementation and my original recommendation — is not installable here at
all. `rapidocr-onnxruntime` runs the same PP-OCR models via onnxruntime and **bundles them
in the wheel**, whereas `rapidocr` 3.x downloads them at runtime. Under a no-egress
constraint a self-contained wheel is the requirement, not a convenience.

The engine is behind an ABC. Swapping it touches one adapter.

## Measured on synthetic fixtures only

On a rendered-text fixture at 200 dpi, RapidOCR reads the page correctly at ~0.88 mean
confidence in ~4 s/page on CPU, and the output classifies as `sale_deed`.

**This says nothing about real Mumbai deeds.** The fixture is cleanly rendered digital
text, which is the easiest possible input — no scanner noise, no skew, no stamps, no
Devanagari, no handwriting. Accuracy numbers require the real corpus. What these tests
establish is that the *plumbing* is correct, not that recognition is good enough.

## Not yet built

- Layout analysis, tables, reading order for multi-column pages.
- Devanagari recognition is untested; the bundled models support multiple scripts but no
  Marathi document has been through this.
- GPU execution (onnxruntime is CPU-only here).
- Skew/rotation correction before recognition.
- Any accuracy measurement (CER/WER) — needs ground truth.
