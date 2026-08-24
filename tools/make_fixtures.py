#!/usr/bin/env python3
"""Generate synthetic document fixtures.

Tier-1 development data per docs/privacy/data-handling-policy.md: fully synthetic, safe to
commit, safe to discuss, safe to share. Contains no real names, properties or transactions.

These exercise the shapes that matter to the pipeline — a clean digital text layer, a
good scan, a poor scan, a phone photo — so that tooling can be validated without touching
real collateral documents.

KNOWN LIMITATION: synthetic fixtures will OVERSTATE extraction accuracy. Real-world scan
quality, layout variance and language mixing are exactly what they fail to reproduce.
Real-document evaluation remains a hard gate before any pilot.

USAGE
-----
    python tools/make_fixtures.py fixtures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover
    sys.exit("Pillow required: pip install -r tools/requirements.txt")

A4_PORTRAIT_PT = (595, 842)
A4_INCHES = (8.27, 11.69)


def text_pdf(path: Path, pages: int, line: str, lines_per_page: int = 34) -> None:
    """Build a minimal PDF carrying a genuine text layer.

    Hand-built rather than using a PDF library, so fixture generation adds no dependency
    beyond Pillow. The structure is deliberately plain: catalog, page tree, one content
    stream per page, one shared Helvetica font.
    """
    objs: list[bytes] = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())

    font_obj = 3 + 2 * pages
    w, h = A4_PORTRAIT_PT
    for i in range(pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/Contents {3 + pages + i} 0 R >>".encode()
        )

    for i in range(pages):
        rows = [b"BT /F1 11 Tf 50 780 Td"]
        for r in range(lines_per_page):
            rows.append(f"({line} clause {r + 1} page {i + 1}) Tj 0 -22 Td".encode())
        rows.append(b"ET")
        body = b"\n".join(rows)
        objs.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")

    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()

    path.write_bytes(bytes(buf))


def scan_page(width: int, height: int, blur: float, dpi: int) -> "Image.Image":
    """A page-shaped image standing in for a scanned document.

    Horizontal rules stand in for text lines; the red box stands in for a registration
    stamp or seal. Enough structure for sharpness and layout tooling to have something
    real to measure.
    """
    im = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(im)
    for y in range(80, height - 80, 34):
        d.line([(70, y), (width - 70, y)], fill=(30, 30, 30), width=3)
    d.rectangle(
        [width - 260, height - 200, width - 70, height - 70],
        outline=(120, 20, 20),
        width=5,
    )
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    im.info["dpi"] = (dpi, dpi)
    return im


def scanned_pdf(path: Path, pages: int, blur: float, dpi: int) -> None:
    px = (int(A4_INCHES[0] * dpi), int(A4_INCHES[1] * dpi))
    imgs = [scan_page(*px, blur, dpi) for _ in range(pages)]
    imgs[0].save(
        path, save_all=True, append_images=imgs[1:], resolution=float(dpi)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic document fixtures.")
    ap.add_argument("out", nargs="?", default="fixtures", help="Output directory")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Clean digital text layer — should classify DIGITAL and bypass OCR entirely.
    text_pdf(out / "digital_clean.pdf", 12,
             "AGREEMENT OF SALE between party A and party B")
    text_pdf(out / "digital_short.pdf", 2,
             "PROPERTY TAX RECEIPT assessment number")

    # Scans at differing quality — should classify SCANNED with very different sharpness.
    scanned_pdf(out / "scan_good.pdf", pages=6, blur=0.0, dpi=300)
    scanned_pdf(out / "scan_poor.pdf", pages=4, blur=2.4, dpi=120)

    # Standalone image, as a phone photo of a possession document would arrive.
    scan_page(1240, 1754, blur=1.0, dpi=150).save(
        out / "possession_photo.jpg", dpi=(150, 150)
    )

    names = sorted(p.name for p in out.iterdir())
    print(f"Wrote {len(names)} fixtures to {out}:")
    for n in names:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
