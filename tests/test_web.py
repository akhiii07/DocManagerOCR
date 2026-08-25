"""Tests for the review UI.

The behaviours worth pinning are the ones the design conversation settled:

* the box check has **three** outcomes, and `UNKNOWN` is not "wrong"
* a confident type mismatch is the ONE place gating is correct
* everything else advances, so case-level findings appear even when a document is unhappy
* findings are labelled advisory, because every rule is still DRAFT
* the server refuses to bind anywhere but loopback
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")

from fastapi.testclient import TestClient  # noqa: E402

from dmocr.web import service as web_service  # noqa: E402
from dmocr.web.app import app  # noqa: E402
from dmocr.web.service import BOXES, OTHER_BOX, BoxStatus, ReviewSession  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """A fresh session per test, with uploads processed synchronously.

    The app processes in a background thread so the browser is not blocked by OCR. In
    tests that would be a race, so `threading.Thread` is patched to run inline.
    """
    from dmocr.web import app as web_app

    session = ReviewSession()
    monkeypatch.setattr(web_app, "session", session)

    class Inline:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            self._target, self._args = target, args

        def start(self):
            if self._target:
                self._target(*self._args)

    monkeypatch.setattr(web_app.threading, "Thread", Inline)
    with TestClient(app) as c:
        c.session = session          # type: ignore[attr-defined]
        yield c


def upload(client, box: str, path: Path):
    with path.open("rb") as fh:
        return client.post("/api/upload", data={"box": box},
                           files={"file": (path.name, fh, "application/pdf")})


def box_state(client, key: str) -> dict:
    state = client.get("/api/state").json()
    return next(b for b in state["boxes"] if b["key"] == key)


# =====================================================================================
# Board
# =====================================================================================


class TestBoard:
    def test_page_renders_with_the_named_boxes(self, client):
        html = client.get("/").text
        assert "Sale Deed" in html
        assert "Agreement of Sale" in html
        assert "Property Tax" in html

    def test_property_papers_is_not_a_box(self, client):
        """It has no classifier signals and no schema, so it would fail its own check."""
        assert "Property Papers" not in client.get("/").text
        assert all(t.value != "property_papers" for t, _, _ in BOXES)

    def test_an_unvalidated_other_tray_exists(self, client):
        keys = {b["key"] for b in client.get("/api/state").json()["boxes"]}
        assert OTHER_BOX in keys

    def test_draft_status_is_stated_on_the_page(self, client):
        """Rules are unapproved; the UI must not imply otherwise."""
        html = client.get("/").text
        assert "DRAFT" in html and "advisory" in html

    def test_empty_board_has_no_findings(self, client):
        state = client.get("/api/state").json()
        assert state["documents_present"] == 0
        assert not state["processing"]


# =====================================================================================
# The box check - three outcomes
# =====================================================================================


class TestBoxCheck:
    def test_right_document_in_the_right_box_passes(self, client, bundle_dir: Path):
        assert upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf").is_success
        box = box_state(client, "sale_deed")
        assert box["status"] == BoxStatus.OK.value
        assert any(s["key"] == "classify" and s["status"] == "ok" for s in box["stages"])

    def test_wrong_document_is_held_for_confirmation_not_extracted(
        self, client, bundle_dir: Path
    ):
        """The one place gating is correct: the wrong schema yields confidently wrong
        values, so nothing is read until a human decides."""
        upload(client, "sale_deed", bundle_dir / "bundle_property_tax.pdf")
        box = box_state(client, "sale_deed")
        assert box["status"] == BoxStatus.NEEDS_CONFIRMATION.value
        assert box["suggested_type"] == "property_tax"
        assert box["fields"] == []
        assert "Property Tax" in box["issues"][0]

    def test_unidentifiable_document_is_not_called_wrong(self, client, fixtures_dir: Path):
        """UNKNOWN is a correct answer, not an error. The upload still proceeds."""
        upload(client, "sale_deed", fixtures_dir / "scan_good.pdf")
        box = box_state(client, "sale_deed")
        assert box["status"] != BoxStatus.NEEDS_CONFIRMATION.value
        classify = next(s for s in box["stages"] if s["key"] == "classify")
        assert classify["status"] == "attention"
        assert "could not be checked" in classify["detail"] or \
               "not clearly identif" in classify["detail"] or \
               "could not be determined" in classify["detail"]

    def test_other_tray_never_claims_a_mismatch(self, client, bundle_dir: Path):
        upload(client, OTHER_BOX, bundle_dir / "bundle_property_tax.pdf")
        box = box_state(client, OTHER_BOX)
        assert box["status"] != BoxStatus.NEEDS_CONFIRMATION.value

    def test_unreadable_upload_is_blocked_with_a_reason(self, client, tmp_path: Path):
        bad = tmp_path / "evil.pdf"
        bad.write_bytes(b"%PDF-1.4\n<< /JavaScript (x) >>\n%%EOF\n")
        upload(client, "sale_deed", bad)
        box = box_state(client, "sale_deed")
        assert box["status"] == BoxStatus.BLOCKED.value
        assert box["issues"]


# =====================================================================================
# Confirm and move
# =====================================================================================


class TestUserDecisions:
    def test_confirming_the_type_proceeds_to_extraction(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_property_tax.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]

        client.post("/api/confirm", data={"document_id": doc_id})
        box = box_state(client, "sale_deed")
        assert box["status"] != BoxStatus.NEEDS_CONFIRMATION.value
        assert any("confirmed by reviewer" in i.lower() for i in box["issues"])

    def test_moving_reprocesses_in_the_right_box(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_property_tax.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]

        client.post("/api/move", data={"document_id": doc_id, "target": "property_tax"})
        assert box_state(client, "sale_deed")["document_id"] is None
        moved = box_state(client, "property_tax")
        assert moved["document_id"] == doc_id
        assert moved["status"] == BoxStatus.OK.value

    def test_removing_clears_the_box(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]
        client.post("/api/remove", data={"document_id": doc_id})
        assert box_state(client, "sale_deed")["document_id"] is None

    def test_replacing_a_document_does_not_leave_two_answers(self, client, bundle_dir):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        boxes = client.get("/api/state").json()["boxes"]
        assert sum(1 for b in boxes if b["key"] == "sale_deed") == 1

    def test_reset_clears_everything(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        client.post("/api/reset")
        state = client.get("/api/state").json()
        assert state["documents_present"] == 0


# =====================================================================================
# Case-level findings still appear
# =====================================================================================


class TestCaseFindings:
    def test_cross_document_conflict_surfaces(self, client, bundle_dir: Path):
        """The area conflict needs BOTH documents - it cannot be seen from either alone."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        upload(client, "property_tax", bundle_dir / "bundle_property_tax.pdf")

        findings = client.get("/api/state").json()["findings"]
        area = next(f for f in findings if f["rule_id"] == "XDOC_AREA_001")
        assert area["determination"] == "MISMATCH"
        assert area["disposition"] == "BLOCKER"

    def test_findings_appear_even_when_a_box_needs_attention(self, client, bundle_dir):
        """Gating downstream work would hide these."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        upload(client, "property_tax", bundle_dir / "bundle_property_tax.pdf")
        # Put something unidentifiable in a third box.
        upload(client, "agreement_of_sale", bundle_dir / "bundle_property_tax.pdf")

        state = client.get("/api/state").json()
        assert any(f["rule_id"] == "XDOC_AREA_001" for f in state["findings"])

    def test_missing_required_document_is_reported(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        findings = client.get("/api/state").json()["findings"]
        completeness = next(f for f in findings if f["rule_id"] == "DOC_COMPLETENESS_001")
        assert completeness["determination"] == "MISSING"

    def test_all_findings_are_marked_advisory(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        findings = client.get("/api/state").json()["findings"]
        assert findings and all(f["advisory"] for f in findings)

    def test_business_rules_are_distinguished_from_regulatory(self, client, bundle_dir):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        findings = client.get("/api/state").json()["findings"]
        area = next(f for f in findings if f["rule_id"] == "XDOC_AREA_001")
        ownership = next(f for f in findings if f["rule_id"] == "OWNERSHIP_001")
        assert area["regulatory"] is False        # sound practice, not mandated
        assert ownership["regulatory"] is True    # cites TPA s.54


# =====================================================================================
# Fields and evidence
# =====================================================================================


class TestFieldsAndEvidence:
    def test_fields_carry_value_confidence_and_page(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        fields = box_state(client, "sale_deed")["fields"]
        assert fields
        for f in fields:
            assert f["value"] and f["confidence"] and f["page"] >= 1

    def test_every_field_links_to_its_evidence(self, client, bundle_dir: Path):
        """Values without 'show me where' throw away the main asset."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        fields = box_state(client, "sale_deed")["fields"]
        assert all(f["evidence"] for f in fields)

    def test_evidence_endpoint_returns_a_png_crop(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        field = box_state(client, "sale_deed")["fields"][0]
        res = client.get(field["evidence"])
        assert res.is_success
        assert res.headers["content-type"] == "image/png"
        assert res.content.startswith(b"\x89PNG")

    def test_evidence_without_a_box_falls_back_to_the_page(self, client, bundle_dir):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]
        res = client.get(f"/evidence/{doc_id}/1")
        assert res.is_success and res.content.startswith(b"\x89PNG")

    def test_unknown_document_evidence_is_404(self, client):
        assert client.get("/evidence/nope/1").status_code == 404


# =====================================================================================
# Upload validation and safety
# =====================================================================================


class TestUploadValidation:
    def test_unknown_box_is_rejected(self, client, bundle_dir: Path):
        with (bundle_dir / "bundle_sale_deed.pdf").open("rb") as fh:
            res = client.post("/api/upload", data={"box": "not_a_box"},
                              files={"file": ("x.pdf", fh, "application/pdf")})
        assert res.status_code == 400

    def test_empty_file_is_rejected(self, client):
        res = client.post("/api/upload", data={"box": "sale_deed"},
                          files={"file": ("x.pdf", b"", "application/pdf")})
        assert res.status_code == 400


class TestBinding:
    def test_serve_refuses_a_non_loopback_host(self):
        """No authentication (ADR-0002), so the network boundary is the control."""
        from dmocr.web.app import serve

        with pytest.raises(SystemExit, match="refusing to bind"):
            serve("0.0.0.0", 8000)

    def test_serve_refuses_a_hostname(self):
        from dmocr.web.app import serve

        with pytest.raises(SystemExit):
            serve("example.com", 8000)


class TestServiceUnits:
    def test_box_label_covers_every_box(self):
        for doc_type, label, _ in BOXES:
            assert web_service.box_label(doc_type.value) == label
        assert web_service.box_label(OTHER_BOX) == "Other documents"
