#!/usr/bin/env python3
"""Phase 0 corpus survey.

Answers the questions that decide the OCR and extraction strategy, before any of it is
built:

  * How many documents already carry a usable text layer? (Never OCR what you can read.)
  * Of the rest, what is the scan resolution and sharpness distribution?
  * How long are the documents, and how much does page count vary?
  * How much non-Latin script is present?
  * How many are encrypted, rotated, or otherwise awkward?

Measurement is delegated to `dmocr.ingest.pdfinfo`, which is also what the production
quality gate uses. That is deliberate: a threshold tuned against these survey numbers has
to mean the same thing at ingest time, and two copies of the analysis code would drift.

PRIVACY
-------
No network calls, and no document text is ever written. Output is counts and
distributions only. File names are hashed by default, because file names in a lending
workflow routinely contain borrower names; pass --show-names only if you have checked
that they do not.

See docs/privacy/data-handling-policy.md.

USAGE
-----
    python tools/corpus_survey.py "D:/path/to/documents" --out survey-output
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dmocr.ingest import pdfinfo  # noqa: E402

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


@dataclass
class DocRecord:
    file_id: str
    ext: str
    size_bytes: int
    name: str | None = None

    ok: bool = True
    error: str | None = None
    encrypted: bool = False

    page_count: int | None = None
    total_text_chars: int = 0
    median_chars_per_page: float | None = None
    text_layer_class: str | None = None

    devanagari_chars: int = 0
    other_indic_chars: int = 0
    non_latin_ratio: float | None = None

    min_embedded_dpi: float | None = None
    median_sharpness: float | None = None

    rotated_pages: int = 0
    distinct_page_sizes: int = 0
    producer: str | None = None
    page_sizes_pt: list[list[float]] = field(default_factory=list)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def to_record(path: Path, show_names: bool) -> DocRecord:
    rec = DocRecord(
        file_id=file_sha256(path)[:16],
        ext=path.suffix.lower(),
        size_bytes=path.stat().st_size,
        name=path.name if show_names else None,
    )
    info = pdfinfo.analyse(path)

    rec.ok, rec.error, rec.encrypted = info.ok, info.error, info.encrypted
    rec.page_count = info.page_count or None
    rec.total_text_chars = info.total_text_chars
    rec.median_chars_per_page = info.median_chars_per_page
    rec.text_layer_class = info.text_layer
    rec.devanagari_chars = info.devanagari_chars
    rec.other_indic_chars = info.other_indic_chars
    rec.non_latin_ratio = info.non_latin_ratio
    rec.min_embedded_dpi = info.min_embedded_dpi
    rec.median_sharpness = info.median_sharpness
    rec.rotated_pages = info.rotated_pages
    rec.distinct_page_sizes = info.distinct_page_sizes
    rec.producer = info.producer
    rec.page_sizes_pt = [
        [round(p.width_pt or 0, 1), round(p.height_pt or 0, 1)] for p in info.pages
    ]
    return rec


# --------------------------------------------------------------------------------------
# Aggregation and reporting
# --------------------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(float(s[int(k)]), 2)
    return round(float(s[lo] + (s[hi] - s[lo]) * (k - lo)), 2)


def summarise(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 2),
        "p10": percentile(values, 0.10),
        "median": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "max": round(max(values), 2),
    }


def aggregate(records: list[DocRecord]) -> dict[str, Any]:
    ok = [r for r in records if r.ok]
    failed = [r for r in records if not r.ok]

    text_class = Counter(r.text_layer_class or "UNKNOWN" for r in ok)
    page_counts = [float(r.page_count) for r in ok if r.page_count]
    dpis = [float(r.min_embedded_dpi) for r in ok if r.min_embedded_dpi]
    sharp = [float(r.median_sharpness) for r in ok if r.median_sharpness is not None]
    non_latin = [r.non_latin_ratio for r in ok if r.non_latin_ratio is not None]

    n_ok = len(ok) or 1
    digital = text_class.get(pdfinfo.TextLayer.DIGITAL, 0)

    return {
        "documents_total": len(records),
        "documents_ok": len(ok),
        "documents_failed": len(failed),
        "failures_by_reason": dict(Counter(r.error or "unknown" for r in failed)),
        "encrypted": sum(1 for r in records if r.encrypted),
        "extensions": dict(Counter(r.ext for r in records)),
        "text_layer_class": dict(text_class),
        "text_layer_digital_pct": round(100.0 * digital / n_ok, 1),
        "page_count": summarise(page_counts),
        "total_pages": int(sum(page_counts)),
        "min_embedded_dpi": summarise(dpis),
        "dpi_below_200": sum(1 for d in dpis if d < 200),
        "dpi_below_150": sum(1 for d in dpis if d < 150),
        "sharpness_laplacian_var": summarise(sharp),
        "non_latin_ratio": summarise([v * 100 for v in non_latin]),
        "documents_with_any_devanagari": sum(1 for r in ok if r.devanagari_chars > 0),
        "documents_with_rotated_pages": sum(1 for r in ok if r.rotated_pages),
        "documents_with_mixed_page_sizes": sum(1 for r in ok if r.distinct_page_sizes > 1),
        "producers_top": dict(Counter(r.producer for r in ok if r.producer).most_common(10)),
    }


def render_markdown(agg: dict[str, Any], root: str, started: str) -> str:
    L: list[str] = []
    A = L.append
    A("# Phase 0 - Corpus Survey")
    A("")
    A(f"- **Source root (hashed):** `{sha256_of(root)[:16]}`")
    A(f"- **Run at:** {started}")
    A(f"- **Documents:** {agg['documents_total']} "
      f"({agg['documents_ok']} readable, {agg['documents_failed']} failed, "
      f"{agg['encrypted']} encrypted)")
    A(f"- **Total pages:** {agg['total_pages']}")
    A("")

    A("## The question that decides the OCR strategy")
    A("")
    A(f"**{agg['text_layer_digital_pct']}% of readable documents already carry a usable "
      "text layer.**")
    A("")
    for k, v in sorted(agg["text_layer_class"].items(), key=lambda kv: -kv[1]):
        A(f"- `{k}`: {v}")
    A("")
    A("> `DIGITAL` documents should bypass OCR entirely - extracting the embedded text")
    A("> layer is both more accurate and far cheaper. `MIXED` needs per-page routing, not")
    A("> per-document. Only `SCANNED` requires the full OCR path.")
    A("")

    A("## Scan quality (drives the quality gate thresholds)")
    A("")
    A(f"- Embedded image DPI: `{agg['min_embedded_dpi']}`")
    A(f"- Documents below 200 dpi: **{agg['dpi_below_200']}**")
    A(f"- Documents below 150 dpi: **{agg['dpi_below_150']}**")
    A(f"- Sharpness (Laplacian variance, relative): `{agg['sharpness_laplacian_var']}`")
    A("")
    A("> Sharpness is comparable only within this run. Use the p10 value as the starting")
    A("> point for `QualityThresholds.min_sharpness`, then tune against measured OCR")
    A("> accuracy - not against the number itself.")
    A("")

    A("## Document length")
    A("")
    A(f"- Pages per document: `{agg['page_count']}`")
    A("")
    A("> The p90 drives per-case GPU cost and latency. If it is large, page-level")
    A("> relevance filtering before VLM extraction stops being an optimisation and")
    A("> becomes a requirement.")
    A("")

    A("## Language and script")
    A("")
    A(f"- Documents containing Devanagari: **{agg['documents_with_any_devanagari']}**")
    A(f"- Non-Latin character share, %: `{agg['non_latin_ratio']}`")
    A("")
    A("> Measured on the text layer only, so it understates scanned Marathi content.")
    A("> Treat as a lower bound.")
    A("")

    A("## Awkward cases")
    A("")
    A(f"- Rotated pages: **{agg['documents_with_rotated_pages']}** documents")
    A(f"- Mixed page sizes within one document: **{agg['documents_with_mixed_page_sizes']}**")
    A(f"- Failures: `{agg['failures_by_reason']}`")
    A("")
    A("## Extensions")
    A("")
    A(f"`{agg['extensions']}`")
    A("")
    A("## PDF producers")
    A("")
    A(f"`{agg['producers_top']}`")
    A("")
    A("> A dominant producer suggests a consistent source system, which usually means")
    A("> consistent layout and easier extraction.")
    A("")
    return "\n".join(L)


# --------------------------------------------------------------------------------------


def iter_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (PDF_EXT | IMAGE_EXT) and p not in seen:
            seen.add(p)
            yield p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 0 corpus survey (local only, no network).")
    ap.add_argument("root", help="Directory containing the document corpus")
    ap.add_argument("--out", default="survey-output", help="Output directory")
    ap.add_argument("--show-names", action="store_true",
                    help="Include real file names. Only use if names contain no PII.")
    ap.add_argument("--limit", type=int, default=0, help="Survey at most N documents")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    if not pdfinfo.HAVE_PDFIUM:
        print("warning: pypdfium2 missing - PDFs will be skipped.", file=sys.stderr)
    if not pdfinfo.HAVE_NUMPY:
        print("warning: numpy missing - sharpness metrics disabled.", file=sys.stderr)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    files = list(iter_files(root))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"error: no PDF or image files found under {root}", file=sys.stderr)
        return 1

    records: list[DocRecord] = []
    for n, path in enumerate(files, 1):
        try:
            records.append(to_record(path, args.show_names))
        except Exception as exc:  # never let one bad file end the survey
            traceback.print_exc(file=sys.stderr)
            records.append(DocRecord(
                file_id=sha256_of(str(path))[:16], ext=path.suffix.lower(),
                size_bytes=0, ok=False, error=f"unhandled: {type(exc).__name__}",
            ))
        print(f"\r  surveyed {n}/{len(files)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    agg = aggregate(records)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 2,
        "started_at": started,
        "root_hash": sha256_of(str(root)),
        "names_included": bool(args.show_names),
        "thresholds": {
            "digital_min_chars_per_page": pdfinfo.DIGITAL_MIN_CHARS_PER_PAGE,
            "scanned_max_chars_per_page": pdfinfo.SCANNED_MAX_CHARS_PER_PAGE,
            "render_scale": pdfinfo.RENDER_SCALE,
        },
        "aggregate": agg,
        "documents": [asdict(r) for r in records],
    }
    (out / "corpus-survey.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "corpus-survey.md").write_text(
        render_markdown(agg, str(root), started), encoding="utf-8")

    print(f"\nWrote {out / 'corpus-survey.json'}")
    print(f"Wrote {out / 'corpus-survey.md'}")
    print(f"\n{agg['documents_ok']}/{agg['documents_total']} readable - "
          f"{agg['total_pages']} pages - "
          f"{agg['text_layer_digital_pct']}% already have a text layer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
