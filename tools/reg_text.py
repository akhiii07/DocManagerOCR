#!/usr/bin/env python3
"""Regulatory text extraction and search.

B1 tooling. Locates provisions inside large primary instruments (Master Directions, Acts)
so that requirements can be extracted with exact page references and verbatim quotes.

This operates ONLY on public regulatory documents in docs/regulatory/sources/. It is not
part of the customer document pipeline and must never be pointed at a collateral document —
it prints extracted text to stdout, which is exactly what the data-handling policy forbids
for customer content.

USAGE
-----
    python tools/reg_text.py identify docs/regulatory/sources
    python tools/reg_text.py search docs/regulatory/sources/rbi-hfc-md-2021.pdf "loan.to.value|LTV"
    python tools/reg_text.py page   docs/regulatory/sources/rbi-hfc-md-2021.pdf 42
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover
    sys.exit("pypdfium2 required: pip install -r tools/requirements.txt")

GUARD_DIR = "docs/regulatory/sources"


def _guard(path: Path) -> None:
    """Refuse to run against anything outside the public regulatory corpus."""
    norm = path.as_posix().lower()
    if GUARD_DIR not in norm:
        sys.exit(
            f"refusing: {path}\n"
            f"reg_text.py prints extracted text and may only be used on public regulatory\n"
            f"documents under {GUARD_DIR}/. See docs/privacy/data-handling-policy.md"
        )


def page_texts(path: Path) -> list[str]:
    pdf = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(pdf)):
            try:
                out.append(pdf[i].get_textpage().get_text_bounded())
            except Exception:
                out.append("")
        return out
    finally:
        pdf.close()


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def cmd_identify(args) -> int:
    root = Path(args.path)
    if root.is_dir():
        # dict.fromkeys dedupes: on case-insensitive filesystems *.pdf and *.PDF
        # both match every file.
        files = sorted(dict.fromkeys(list(root.glob("*.pdf")) + list(root.glob("*.PDF"))))
    else:
        files = [root]
    for f in files:
        _guard(f)
        try:
            pages = page_texts(f)
        except Exception as exc:
            print(f"\n## {f.name}\n  ERROR: {type(exc).__name__}: {exc}")
            continue
        chars = sum(len(p) for p in pages)
        head = collapse(pages[0])[:400] if pages else ""
        print(f"\n## {f.name}")
        print(f"  pages: {len(pages)}   text chars: {chars:,}"
              f"   {'TEXT LAYER OK' if chars > 1000 else '*** LITTLE/NO TEXT — may need OCR ***'}")
        print(f"  p1: {head}")
    return 0


def cmd_search(args) -> int:
    path = Path(args.path)
    _guard(path)
    pages = page_texts(path)
    try:
        rx = re.compile(args.pattern, re.IGNORECASE)
    except re.error as exc:
        sys.exit(f"bad regex: {exc}")

    hits = 0
    for n, text in enumerate(pages, 1):
        if not text:
            continue
        flat = collapse(text)
        for m in rx.finditer(flat):
            hits += 1
            if hits > args.limit:
                print(f"\n... stopped at --limit {args.limit}")
                return 0
            a = max(0, m.start() - args.context)
            b = min(len(flat), m.end() + args.context)
            print(f"\n[p{n}] ...{flat[a:b]}...")
    if not hits:
        print(f"no match for {args.pattern!r} in {path.name} ({len(pages)} pages)")
    else:
        print(f"\n-- {hits} hit(s) across {len(pages)} pages --")
    return 0


def cmd_page(args) -> int:
    path = Path(args.path)
    _guard(path)
    pages = page_texts(path)
    lo = max(1, args.number)
    hi = min(len(pages), args.to or args.number)
    for n in range(lo, hi + 1):
        print(f"\n=============== {path.name} p{n}/{len(pages)} ===============")
        print(pages[n - 1])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Search public regulatory PDFs (B1 tooling).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_id = sub.add_parser("identify", help="page counts and first-page snippets")
    p_id.add_argument("path")
    p_id.set_defaults(func=cmd_identify)

    p_s = sub.add_parser("search", help="regex search, reports page numbers")
    p_s.add_argument("path")
    p_s.add_argument("pattern")
    p_s.add_argument("--context", type=int, default=320)
    p_s.add_argument("--limit", type=int, default=40)
    p_s.set_defaults(func=cmd_search)

    p_p = sub.add_parser("page", help="dump text of a page or page range")
    p_p.add_argument("path")
    p_p.add_argument("number", type=int)
    p_p.add_argument("--to", type=int, default=None)
    p_p.set_defaults(func=cmd_page)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
