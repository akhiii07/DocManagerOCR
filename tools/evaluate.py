#!/usr/bin/env python3
"""Run the evaluation harness over a labelled corpus.

    python tools/evaluate.py --truth eval/groundtruth --documents fixtures/bundle

PRIVACY
-------
Ground truth for real documents contains transcribed customer data. Keep it OUTSIDE the
repository and off any backup that leaves the machine.

The report is safe to circulate by default: metrics, counts and identifiers, never values.
`--show-values` includes expected and extracted values for local debugging only - the
resulting file is customer content and must be handled as such.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dmocr.eval import (  # noqa: E402
    EvaluationRunner,
    check_gates,
    load_corpus,
    render_markdown,
    write_report,
)
from dmocr.eval.report import as_dict  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the pipeline against ground truth.")
    ap.add_argument("--truth", required=True, help="Directory of ground-truth YAML files")
    ap.add_argument("--documents", required=True, help="Directory containing the documents")
    ap.add_argument("--out", default="eval-output", help="Report output directory")
    ap.add_argument("--show-values", action="store_true",
                    help="Include values in the report. LOCAL DEBUGGING ONLY - the output "
                         "becomes customer content.")
    ap.add_argument("--gates", action="store_true",
                    help="Apply regression gates and exit non-zero on failure")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.ERROR if args.quiet else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    corpus = load_corpus(args.truth)
    if not corpus:
        print(f"error: no ground truth found in {args.truth}", file=sys.stderr)
        return 2

    runner = EvaluationRunner(include_values=args.show_values)
    result = runner.run(corpus, args.documents)

    json_path, md_path = write_report(result, args.out)
    if not args.quiet:
        print(render_markdown(result))
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    if args.show_values:
        print("\nWARNING: report includes values and must be treated as customer content.")

    if not args.gates:
        return 0

    gate_report = check_gates(as_dict(result))
    print("\nREGRESSION GATES")
    for r in gate_report.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.gate.name}: {r.reason}")
    if not gate_report.passed:
        print(f"\n{len(gate_report.failures)} gate(s) failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
