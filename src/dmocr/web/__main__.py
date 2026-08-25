"""python -m dmocr.web"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the collateral review UI.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Loopback only. There is no authentication (ADR-0002).")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    from .app import serve

    print(f"Review UI on http://{args.host}:{args.port}")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
