# Review UI

`src/dmocr/web/` · tests in `tests/test_web.py`

```bash
python -m dmocr.web          # http://127.0.0.1:8000
```

**Localhost by default.** There is no user authentication (ADR-0002), so the network
boundary is the control.

## Sharing it: the guard is conditional, not removed

The tempting way to share this is to drop the loopback guard — which would leave an
**unauthenticated document-upload endpoint** exposed. So the guard is conditional instead:

```
non-loopback binding is allowed ONLY when an access token is configured
```

Never neither. Four tests pin it.

```bash
python -m dmocr.web --public                      # generates a token, prints the URL
cloudflared tunnel --url http://127.0.0.1:8000    # in another terminal
```

Share `https://<name>.trycloudflare.com/?token=<token>`. The first request swaps the
token for an `HttpOnly` cookie and redirects to a clean URL, so the secret stops travelling
in the address bar and referrer headers.

Access is enforced by **middleware, not a per-route dependency**, so static assets and any
future endpoint are covered by default — a new route cannot become public by omission.
Only `/healthz` is open, so a tunnel can probe it without the secret.

### Why this is demo-only, stated plainly

Public mode forces a red banner saying so, because a shared token is not authentication:

- one token means **one identity for everyone** — the audit ledger cannot attribute anything to a person
- **no revocation** short of restarting with a new token
- a token in a URL leaks through browser history and shoulder-surfing

Processing still happens **on your machine** — documents never reach a third-party host,
which is why a tunnel beats any PaaS here. But real collateral documents need real
authentication (OPEN-ITEMS 13) and a private deployment. Not a tunnel.

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

## Field confirmation

Each extracted field carries **accept** and **correct**. This is the "data confirmation"
step, and it does more than tidy the display — it is the only ground truth the system will
ever generate from real use, and therefore the input to confidence calibration.

**A correction is a new claim, not an edit.** Claims are immutable (ADR-0003), so a
correction becomes a claim with `HumanProvenance` and the original stays in the feedback
log. A human claim is legitimately **ungrounded**: ADR-0004 constrains what the *model* may
assert, not what a reviewer may. The provenance kind records which it was.

Corrections **flow into the case checks**. Correcting the deed's area to agree with the tax
bill clears the cross-document conflict — there's a test for exactly that round trip.

Three details that matter:

- **Evidence still points at what the system read**, even after an override, so a reviewer
  can check the original.
- **Correcting an area keeps its measurement basis.** Otherwise correcting a number would
  silently drop "carpet" and make the value incomparable with the other documents.
- **An unreadable correction is refused with the reason**, never stored as a guess. A wrong
  value carrying a human's authority is worse than the extraction error being corrected,
  and a refused correction leaves no trace in the log.

### The calibration signal

`GET /api/feedback` reports the **correction rate by the confidence the system claimed**:

```json
{"by_confidence": {"high": {"accepted": 8, "corrected": 2, "correction_rate": 0.2}}}
```

That is the calibration question in one line — *when the system said HIGH, how often was it
wrong?* The outcome alone says nothing; it is only meaningful against the confidence that
was asserted. History is append-only, so changing your mind doesn't erase that you decided
otherwise first.

## Not yet built

- **Operator task list** for T4/T5 external verification.
- **Multi-case and persistence** — the session is single-case and in-memory.
- Side-by-side full document viewer.
