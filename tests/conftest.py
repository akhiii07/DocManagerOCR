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
