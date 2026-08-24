#!/usr/bin/env python3
"""Phase 0 corpus survey.

Answers the questions that decide the OCR and extraction strategy, before any of it is
built:

  * How many documents already carry a usable text layer? (Never OCR what you can read.)
  * Of the rest, what is the scan resolution and sharpness distribution?
  * How long are the documents, and how much does page count vary?
  * How much non-Latin script is present?
  * How many are encrypted, rotated, or otherwise awkward?

PRIVACY
-------
This tool makes NO network calls and never writes document text. It emits counts,
distributions and hashes only. File names are hashed by default, because file names in a
lending workflow routinely contain borrower names; pass --show-names only if you have
checked that they do not.

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

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

# Text-layer classification, in characters per page (median across the document).
DIGITAL_MIN_CHARS_PER_PAGE = 200
SCANNED_MAX_CHARS_PER_PAGE = 20

# Pages sampled per document for the expensive render-based metrics.
RENDER_SAMPLE_PAGES = 5
RENDER_SCALE = 2.0  # 144 dpi equivalent

DEVANAGARI = (0x0900, 0x097F)
# Other Indic blocks worth counting separately if the corpus turns out to be multilingual.
OTHER_INDIC = (0x0980, 0x0DFF)


# --------------------------------------------------------------------------------------
# Optional dependencies. The survey degrades rather than failing.
# --------------------------------------------------------------------------------------

try:
    import pypdfium2 as pdfium  # type: ignore

    HAVE_PDFIUM = True
except Exception:  # pragma: no cover
    HAVE_PDFIUM = False

try:
    import numpy as np  # type: ignore

    HAVE_NUMPY = True
except Exception:  # pragma: no cover
    HAVE_NUMPY = False

try:
    from PIL import Image  # type: ignore

    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False


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
    text_layer_class: str | None = None  # DIGITAL | MIXED | SCANNED | UNKNOWN

    devanagari_chars: int = 0
    other_indic_chars: int = 0
    non_latin_ratio: float | None = None

    embedded_image_dpi: list[float] = field(default_factory=list)
    min_embedded_dpi: float | None = None

    rotations: list[int] = field(default_factory=list)
    page_sizes_pt: list[list[float]] = field(default_factory=list)

    sharpness_samples: list[float] = field(default_factory=list)
    median_sharpness: float | None = None

    producer: str | None = None


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


def count_scripts(text: str) -> tuple[int, int, int]:
    """Return (devanagari, other_indic, latin) character counts."""
    dev = other = latin = 0
    for ch in text:
        cp = ord(ch)
        if DEVANAGARI[0] <= cp <= DEVANAGARI[1]:
            dev += 1
        elif OTHER_INDIC[0] <= cp <= OTHER_INDIC[1]:
            other += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            latin += 1
    return dev, other, latin


def laplacian_variance(gray: "np.ndarray") -> float:
    """Variance of the Laplacian — a standard relative sharpness proxy.

    Higher is sharper. The absolute value is only meaningful compared against other pages
    rendered at the same scale, which is why the report presents a distribution rather
    than a pass/fail threshold.
    """
    g = gray.astype("float32")
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def to_gray(arr: "np.ndarray") -> "np.ndarray":
    if arr.ndim == 2:
        return arr
    if arr.ndim == 3:
        a = arr[:, :, :3].astype("float32")
        return 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    raise ValueError(f"unexpected array shape {arr.shape}")


def sample_indices(n: int, k: int) -> list[int]:
    """Evenly spread k sample indices across n pages (first and last always included)."""
    if n <= k:
        return list(range(n))
    step = (n - 1) / (k - 1)
    return sorted({int(round(i * step)) for i in range(k)})


# --------------------------------------------------------------------------------------
# PDF survey
# --------------------------------------------------------------------------------------


def survey_pdf(path: Path, rec: DocRecord) -> None:
    if not HAVE_PDFIUM:
        rec.ok = False
        rec.error = "pypdfium2 not installed"
        return

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            rec.encrypted = True
            rec.ok = False
            rec.error = "encrypted"
        else:
            rec.ok = False
            rec.error = f"open_failed: {type(exc).__name__}"
        return

    try:
        rec.page_count = len(pdf)

        try:
            meta = pdf.get_metadata_dict()
            rec.producer = (meta.get("Producer") or meta.get("Creator") or None)
        except Exception:
            pass

        per_page_chars: list[int] = []
        dev_total = other_total = latin_total = 0

        for i in range(rec.page_count):
            try:
                page = pdf[i]
            except Exception:
                per_page_chars.append(0)
                continue

            try:
                rec.rotations.append(int(page.get_rotation()))
            except Exception:
                pass

            try:
                w, h = page.get_size()
                rec.page_sizes_pt.append([round(float(w), 1), round(float(h), 1)])
            except Exception:
                pass

            text = ""
            try:
                tp = page.get_textpage()
                text = tp.get_text_bounded()
            except Exception:
                try:
                    text = tp.get_text_range()  # older pypdfium2
                except Exception:
                    text = ""

            per_page_chars.append(len(text))
            d, o, la = count_scripts(text)
            dev_total += d
            other_total += o
            latin_total += la

            # Embedded image resolution — the honest measure of scan quality, better
            # than anything inferred from the rendered page.
            try:
                for obj in page.get_objects():
                    md = getattr(obj, "get_metadata", None)
                    if md is None:
                        continue
                    try:
                        info = md()
                    except Exception:
                        continue
                    for attr in ("horizontal_dpi", "vertical_dpi"):
                        val = getattr(info, attr, None)
                        if val and 10 < float(val) < 2400:
                            rec.embedded_image_dpi.append(round(float(val), 1))
            except Exception:
                pass

        rec.total_text_chars = sum(per_page_chars)
        rec.devanagari_chars = dev_total
        rec.other_indic_chars = other_total

        non_latin = dev_total + other_total
        denom = non_latin + latin_total
        rec.non_latin_ratio = round(non_latin / denom, 4) if denom else None

        if per_page_chars:
            rec.median_chars_per_page = float(statistics.median(per_page_chars))
            m = rec.median_chars_per_page
            if m >= DIGITAL_MIN_CHARS_PER_PAGE:
                rec.text_layer_class = "DIGITAL"
            elif m <= SCANNED_MAX_CHARS_PER_PAGE:
                rec.text_layer_class = "SCANNED"
            else:
                rec.text_layer_class = "MIXED"
        else:
            rec.text_layer_class = "UNKNOWN"

        if rec.embedded_image_dpi:
            rec.min_embedded_dpi = min(rec.embedded_image_dpi)

        # Sharpness only matters for pages we will actually have to OCR.
        if HAVE_NUMPY and rec.text_layer_class in ("SCANNED", "MIXED", "UNKNOWN"):
            for i in sample_indices(rec.page_count, RENDER_SAMPLE_PAGES):
                try:
                    bitmap = pdf[i].render(scale=RENDER_SCALE)
                    arr = bitmap.to_numpy()
                    rec.sharpness_samples.append(round(laplacian_variance(to_gray(arr)), 2))
                except Exception:
                    continue
            if rec.sharpness_samples:
                rec.median_sharpness = float(statistics.median(rec.sharpness_samples))

    finally:
        try:
            pdf.close()
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# Image survey
# --------------------------------------------------------------------------------------


def survey_image(path: Path, rec: DocRecord) -> None:
    if not HAVE_PIL:
        rec.ok = False
        rec.error = "Pillow not installed"
        return
    try:
        with Image.open(path) as im:
            rec.page_count = getattr(im, "n_frames", 1)
            rec.page_sizes_pt.append([float(im.width), float(im.height)])
            dpi = im.info.get("dpi")
            if dpi and dpi[0]:
                rec.embedded_image_dpi.append(round(float(dpi[0]), 1))
                rec.min_embedded_dpi = rec.embedded_image_dpi[0]
            rec.text_layer_class = "SCANNED"
            if HAVE_NUMPY:
                arr = np.asarray(im.convert("RGB"))
                rec.sharpness_samples.append(round(laplacian_variance(to_gray(arr)), 2))
                rec.median_sharpness = rec.sharpness_samples[0]
    except Exception as exc:
        rec.ok = False
        rec.error = f"open_failed: {type(exc).__name__}"


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

    rotated = sum(1 for r in ok if any(x % 360 != 0 for x in r.rotations))
    multi_size = sum(1 for r in ok if len({tuple(s) for s in r.page_sizes_pt}) > 1)

    n_ok = len(ok) or 1
    digital = text_class.get("DIGITAL", 0)

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
        "documents_with_rotated_pages": rotated,
        "documents_with_mixed_page_sizes": multi_size,
        "producers_top": dict(Counter(r.producer for r in ok if r.producer).most_common(10)),
    }


def render_markdown(agg: dict[str, Any], root: str, started: str) -> str:
    L: list[str] = []
    A = L.append
    A("# Phase 0 — Corpus Survey")
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
    A("> Documents classed `DIGITAL` should bypass OCR entirely — extracting the embedded")
    A("> text layer is both more accurate and far cheaper. `MIXED` documents need per-page")
    A("> routing, not per-document. Only `SCANNED` requires the full OCR path.")
    A("")

    A("## Scan quality (drives the quality gate thresholds)")
    A("")
    A(f"- Embedded image DPI: `{agg['min_embedded_dpi']}`")
    A(f"- Documents below 200 dpi: **{agg['dpi_below_200']}**")
    A(f"- Documents below 150 dpi: **{agg['dpi_below_150']}**")
    A(f"- Sharpness (Laplacian variance, relative): `{agg['sharpness_laplacian_var']}`")
    A("")
    A("> Sharpness is comparable only within this run. Use the p10 value as the starting")
    A("> point for the `DEGRADED` threshold in the quality gate, then tune against")
    A("> measured OCR accuracy — not against the number itself.")
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
    A("> Mixed page sizes usually mean appended annexures or differently-scanned")
    A("> sections — a signal that per-page routing matters.")
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
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (PDF_EXT | IMAGE_EXT):
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

    if not HAVE_PDFIUM:
        print("warning: pypdfium2 missing — PDFs will be skipped. "
              "pip install -r tools/requirements.txt", file=sys.stderr)
    if not HAVE_NUMPY:
        print("warning: numpy missing — sharpness metrics disabled.", file=sys.stderr)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records: list[DocRecord] = []

    files = list(iter_files(root))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"error: no PDF or image files found under {root}", file=sys.stderr)
        return 1

    for n, path in enumerate(files, 1):
        ext = path.suffix.lower()
        rec = DocRecord(
            file_id=file_sha256(path)[:16],
            ext=ext,
            size_bytes=path.stat().st_size,
            name=path.name if args.show_names else None,
        )
        try:
            if ext in PDF_EXT:
                survey_pdf(path, rec)
            else:
                survey_image(path, rec)
        except Exception as exc:  # never let one bad file end the survey
            rec.ok = False
            rec.error = f"unhandled: {type(exc).__name__}"
            traceback.print_exc(file=sys.stderr)
        records.append(rec)
        print(f"\r  surveyed {n}/{len(files)}", end="", file=sys.stderr, flush=True)

    print(file=sys.stderr)

    agg = aggregate(records)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "started_at": started,
        "root_hash": sha256_of(str(root)),
        "names_included": bool(args.show_names),
        "thresholds": {
            "digital_min_chars_per_page": DIGITAL_MIN_CHARS_PER_PAGE,
            "scanned_max_chars_per_page": SCANNED_MAX_CHARS_PER_PAGE,
            "render_scale": RENDER_SCALE,
        },
        "aggregate": agg,
        "documents": [asdict(r) for r in records],
    }
    (out / "corpus-survey.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out / "corpus-survey.md").write_text(
        render_markdown(agg, str(root), started), encoding="utf-8"
    )

    print(f"\nWrote {out / 'corpus-survey.json'}")
    print(f"Wrote {out / 'corpus-survey.md'}")
    print(f"\n{agg['documents_ok']}/{agg['documents_total']} readable · "
          f"{agg['total_pages']} pages · "
          f"{agg['text_layer_digital_pct']}% already have a text layer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
