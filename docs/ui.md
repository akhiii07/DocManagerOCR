# Review UI

`src/dmocr/web/` · tests in `tests/test_web.py`

```bash
python -m dmocr.web          # http://127.0.0.1:8000
```

**Localhost only.** There is no authentication (ADR-0002), so the network boundary is the
control. `serve()` refuses to bind to anything but loopback.

## Layout

```
┌─ DOCUMENTS ──────────────┐  ┌─ CASE REVIEW ───────────────────────┐
│ [Agreement of Sale]  ✓   │  │ BLOCKER   Property area conflict    │
│ [Sale Deed]          ✓   │  │   2 distinct values across 3 sources│
│ [Property Tax]       ⚠   │  │ REVIEW    Ownership uncorroborated  │
│ [Possession]     upload  │  │ REVIEW    Missing: possession doc   │
│ [+ Other documents]      │  │                                     │
└──────────────────────────┘  └─────────────────────────────────────┘
```

Two zones, because per-document status alone has nowhere to show the findings that matter
most. **The area conflict between a deed and a tax bill cannot be seen from either
document alone** — it needs the assembled case.

## Why "Property Papers" isn't a box

It has **no classifier signals and no extraction schema** — it's a catch-all label for a
bundle, not a recognisable document, and there are tests pinning both. A box named that
would fail its own "is this the right document?" check every time.

Instead there are four named boxes and an **"Other documents"** tray that accepts anything,
classifies what it can, and never claims a mismatch.

## The box check has three outcomes

| Result | Behaviour |
|---|---|
| Matches the box | ✓ proceed to extraction |
| Confidently a **different** type | **held** — confirm or move |
| `UNKNOWN` | ⚠ proceed on the user's selection, flagged |

`UNKNOWN` is **not** "wrong". The classifier defers by design, and a scanned document is
`UNKNOWN` until OCR has run. Calling that an error would train users to ignore the warning
that matters.

The user's box choice is itself a **human classification, which outranks the classifier's**
— so confirming proceeds, and the disagreement is recorded rather than discarded.

## Gating: right once, wrong everywhere else

The original design gated every step on the previous one. That's correct in exactly one
place and harmful everywhere else.

**Right:** a confident type mismatch. Extracting a Property Tax bill with the Sale Deed
schema produces a full set of plausible, wrong fields — the exact failure this platform
exists to prevent. So nothing is read until a human decides.

**Wrong:** everywhere else. If an uncertain classification blocked the pipeline, the
cross-document conflict would never surface. So stages advance, each reports its own
status, and the case panel recomputes on whatever is available. A test pins this: findings
still appear when another box needs attention.

## Evidence: server-side crops, not PDF.js

Every field links to **the exact region it was read from**, with a red outline marking the
box and surrounding context for orientation.

No PDF.js. We already render pages with `pypdfium2` and hold the bbox in PDF points, so a
server-side crop is a few lines — and a **no-egress deployment cannot pull a viewer from a
CDN** anyway, so the alternative would be vendoring one.

Falls back to the whole page where a value has no block geometry. Showing the page is
better than showing nothing.

## Two things the UI must say out loud

**Every rule is `DRAFT`.** The board runs `DRY_RUN` and labels findings **advisory**. In
`ENFORCE` mode it would show zero findings and look broken; showing them unlabelled would
imply legal approval they don't have.

**Business rules are distinguished from regulatory ones.** A finding with no citations is
tagged `business rule`, not `regulatory`. `XDOC_AREA_001` is sound underwriting practice
that no instrument mandates; `OWNERSHIP_001` cites TPA s.54. The package must not imply
backing a rule doesn't have.

## Timing

Uploads return immediately and the page polls. OCR runs at roughly four seconds a page on
CPU, so a synchronous POST would hang the browser on a forty-page deed.

## Stack

FastAPI + Jinja + plain JavaScript. No build step, no npm, no CDN — everything served
locally, which the privacy constraint requires regardless.

## Not yet built

- **Per-field accept/correct.** Fields show value, confidence and evidence; the
  accept-or-correct interaction is the next pass. It's also the feedback signal for
  confidence calibration, so it matters beyond the UI.
- **Operator task list** for T4/T5 external verification.
- **Multi-case and persistence** — the session is single-case and in-memory.
- Side-by-side full document viewer.
