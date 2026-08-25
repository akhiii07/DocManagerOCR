"""Shared test fixtures.

Uses the synthetic fixture generator in `tools/`. Nothing here touches real customer
documents — see docs/privacy/data-handling-policy.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import make_fixtures  # noqa: E402


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    """Synthetic documents covering DIGITAL, SCANNED-good, SCANNED-poor and a photo."""
    out = tmp_path_factory.mktemp("fixtures")
    make_fixtures.main([str(out)])
    return out


@pytest.fixture(scope="session")
def digital_pdf(fixtures_dir: Path) -> Path:
    return fixtures_dir / "digital_clean.pdf"


@pytest.fixture(scope="session")
def good_scan_pdf(fixtures_dir: Path) -> Path:
    return fixtures_dir / "scan_good.pdf"


@pytest.fixture(scope="session")
def poor_scan_pdf(fixtures_dir: Path) -> Path:
    return fixtures_dir / "scan_poor.pdf"


@pytest.fixture(scope="session")
def photo_jpg(fixtures_dir: Path) -> Path:
    return fixtures_dir / "possession_photo.jpg"


@pytest.fixture(scope="session")
def text_scan_pdf(fixtures_dir: Path) -> Path:
    """Rendered, readable text with no text layer - the OCR path."""
    return fixtures_dir / "text_scan.pdf"


@pytest.fixture(scope="session")
def mixed_bundle_pdf(fixtures_dir: Path) -> Path:
    """Digital text pages with a scanned annexure appended - forces per-page routing."""
    return fixtures_dir / "mixed_bundle.pdf"


@pytest.fixture(scope="session")
def eval_result(fixtures_dir: Path):
    """One evaluation run, shared across the whole session.

    Session-scoped because a run OCRs the scanned fixture, and re-running it per test
    took the eval suite from seconds to nearly six minutes. A slow suite is a suite people
    stop running.
    """
    from dmocr.eval import EvaluationRunner, load_corpus

    return EvaluationRunner().run(load_corpus(Path("eval/groundtruth")), fixtures_dir)


@pytest.fixture(scope="session")
def eval_result_with_values(fixtures_dir: Path):
    """Same run, with values retained - for asserting the opt-in behaves."""
    from dmocr.eval import EvaluationRunner, load_corpus

    return EvaluationRunner(include_values=True).run(
        load_corpus(Path("eval/groundtruth")), fixtures_dir)


@pytest.fixture(scope="session")
def bundle_dir(fixtures_dir: Path) -> Path:
    """Three documents describing ONE property, with a deliberate area conflict.

    All carry a text layer, so cross-document tests need no OCR and stay fast.
    """
    return fixtures_dir / "bundle"
