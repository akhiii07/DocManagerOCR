# Data Handling Policy

**Status:** binding from Phase 0. Applies to development as much as to production.

The realistic breach in a project like this is not a designed violation. It is an engineer
debugging with a real document against a hosted API, a customer name in a log line, or a
prompt containing PII shipped to a SaaS observability tool. This policy is written against
those failures, not against hypothetical attackers.

---

## 1. The hard constraint

**Customer document content must never reach an external AI provider.** Not Claude, not
OpenAI, not Google, not any hosted inference endpoint. This holds in production inference
*and* in development, debugging, evaluation, and demos.

There is no "just this once for testing with one document" exception. If such an exception
existed it would be used, and the boundary would stop being a boundary.

## 2. What "customer document content" means

All of the following are in scope:

- the document file itself (PDF, image, scan)
- rendered page images
- raw or post-processed OCR text
- extracted field values
- canonical model claims
- RAG chunks and their text
- prompts or few-shot examples built from any of the above
- findings text that quotes document content
- **file names and directory paths** — these routinely contain borrower names
- log lines, stack traces, and error messages carrying any of the above

## 3. Where Claude (and any hosted assistant) may and may not be used

**Permitted — this is the whole intended use:**
- architecture, design, code, schemas, review, debugging against synthetic fixtures
- research on public regulatory sources and public portals
- evaluation methodology, rule authoring, prompt design

**Not permitted:**
- pasting a real document, page image, or its OCR output into a chat
- pasting real extracted values, real case data, or real file names
- pointing any production or development pipeline component at a hosted model endpoint

**When debugging a real-document failure**, the workflow is: reproduce against a synthetic or
redacted fixture, and share *that*. If a failure cannot be reproduced without real data, the
correct move is to describe the failure abstractly (shape, dimensions, error class, metric)
rather than to relax the boundary.

## 4. External verification is also an outbound disclosure

Sending a CTS number, owner name, or registration number to MahaRERA, IGR, MCGM or CERSAI is
a disclosure of customer data to a third party. It gets the same scrutiny as an AI API call:
lawful basis, consent scope, contractual controls, data minimisation, logging, retention.

**Minimisation rule:** send the narrowest identifier that resolves the record. If a CTS number
alone resolves a Property Card, do not also send the owner's name.

## 5. Controls to implement, and when

| Control | Phase | Notes |
|---|---|---|
| `.gitignore` blocks document formats and survey output | P0 | Done. A safety net, not the policy. |
| Real documents live outside the repo entirely | P0 | Done by convention; enforce in review. |
| Corpus survey emits aggregate metrics only, never text | P0 | Done. Filenames hashed unless `--show-names`. |
| Egress deny-by-default from processing nodes | P1 | Allowlist: internal services + approved verification sources only. |
| CI check failing on hosted-AI SDK imports in production packages | P1 | Cheap, catches the most likely regression. |
| Log redaction enforced in the logging layer, not at call sites | P1 | Call-site discipline always eventually fails. |
| Self-hosted observability (no SaaS LLM tracing) | P2 | Prompts contain customer data by construction. |
| Encryption at rest with per-tenant keys; TLS in transit | P1 | |
| Retention policy and secure deletion | P9 | Driven by DPDP + RBI requirements confirmed in B1. |

## 6. Development data

Three tiers, in order of preference for any given task:

1. **Synthetic fixtures** — generated, safe to commit and to discuss. Default for unit tests
   and for any debugging conversation.
2. **Redacted real documents** — real layout and language, PII removed. Preferred for
   extraction development.
3. **Real documents** — local machine only. Used for evaluation and accuracy measurement.
   Never leave the machine. Never enter a chat.

Note the known limitation: synthetic documents will **overstate** extraction accuracy,
because real-world scan quality and layout variance are precisely what they fail to
reproduce. Real-document evaluation is a hard gate before any pilot.

## 7. MVP exception on record

Authentication and authorization are out of scope for the MVP (ADR-0002). The compensating
control is that the API surface must bind to localhost or a private network only. An
unauthenticated service handling collateral documents must not be reachable from an
untrusted network.
