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


class TestFieldConfirmation:
    """The confirmation step: accept a value, or correct it.

    Also the only ground truth the system will ever generate from real use, which is why
    the log records the ORIGINAL confidence alongside the outcome.
    """

    def _first_field(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        box = box_state(client, "sale_deed")
        return box["document_id"], box["fields"]

    def test_fields_start_with_no_decision(self, client, bundle_dir: Path):
        _, fields = self._first_field(client, bundle_dir)
        assert all(f["feedback"] is None for f in fields)

    def test_accepting_records_the_decision(self, client, bundle_dir: Path):
        doc_id, fields = self._first_field(client, bundle_dir)
        name = fields[0]["name"]
        assert client.post("/api/accept-field",
                           data={"document_id": doc_id, "field": name}).is_success

        after = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == name)
        assert after["feedback"] == "accepted"

    def test_accepting_does_not_change_the_value(self, client, bundle_dir: Path):
        doc_id, fields = self._first_field(client, bundle_dir)
        field = next(f for f in fields if f["name"] == "consideration")
        client.post("/api/accept-field",
                    data={"document_id": doc_id, "field": "consideration"})
        after = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == "consideration")
        assert after["value"] == field["value"]

    def test_correcting_replaces_the_value_and_keeps_the_original(
        self, client, bundle_dir: Path
    ):
        doc_id, _ = self._first_field(client, bundle_dir)
        res = client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "9900000"})
        assert res.is_success

        field = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == "consideration")
        assert "99,00,000" in field["value"]      # Indian grouping, not 9,900,000
        assert field["feedback"] == "corrected"
        # The system's original reading stays visible.
        assert field["original_value"] is not None
        assert "1,25,00,000" in field["original_value"]

    def test_corrected_value_is_marked_confirmed_not_re_scored(
        self, client, bundle_dir: Path
    ):
        doc_id, _ = self._first_field(client, bundle_dir)
        client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "9900000"})
        field = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == "consideration")
        assert field["confidence"] == "confirmed"

    def test_evidence_still_points_at_what_the_system_read(self, client, bundle_dir):
        """A reviewer must be able to check the original after overriding it."""
        doc_id, _ = self._first_field(client, bundle_dir)
        before = next(f for f in box_state(client, "sale_deed")["fields"]
                      if f["name"] == "consideration")["evidence"]
        client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "9900000"})
        after = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == "consideration")["evidence"]
        assert after == before

    def test_correction_flows_into_cross_document_checks(self, client, bundle_dir: Path):
        """Correcting the area to agree with the tax bill must clear the conflict."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        upload(client, "property_tax", bundle_dir / "bundle_property_tax.pdf")

        findings = client.get("/api/state").json()["findings"]
        assert next(f for f in findings
                    if f["rule_id"] == "XDOC_AREA_001")["determination"] == "MISMATCH"

        doc_id = box_state(client, "sale_deed")["document_id"]
        res = client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "area", "value": "980 sq ft"})
        assert res.is_success

        findings = client.get("/api/state").json()["findings"]
        area = next(f for f in findings if f["rule_id"] == "XDOC_AREA_001")
        assert area["determination"] != "MISMATCH"

    @pytest.mark.parametrize("field,value,fragment", [
        ("consideration", "not a number", "amount"),
        ("execution_date", "the fourteenth", "date"),
        ("area", "1150", "unit"),
    ])
    def test_unreadable_correction_is_refused_with_a_reason(
        self, client, bundle_dir: Path, field, value, fragment
    ):
        """Refuse rather than guess - a wrong value under a human's authority is worse
        than the extraction error being corrected."""
        doc_id, _ = self._first_field(client, bundle_dir)
        res = client.post("/api/correct-field",
                          data={"document_id": doc_id, "field": field, "value": value})
        assert res.status_code == 400
        assert fragment in res.json()["error"].lower()

    def test_refused_correction_leaves_no_trace(self, client, bundle_dir: Path):
        doc_id, _ = self._first_field(client, bundle_dir)
        client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "rubbish"})
        field = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == "consideration")
        assert field["feedback"] is None
        assert client.get("/api/feedback").json()["decisions"] == 0

    def test_empty_correction_is_refused(self, client, bundle_dir: Path):
        doc_id, _ = self._first_field(client, bundle_dir)
        res = client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "   "})
        assert res.status_code == 400

    def test_correcting_an_area_keeps_its_measurement_basis(self, client, bundle_dir):
        """Otherwise correcting a number silently drops 'carpet' and makes the value
        incomparable with the other documents."""
        doc_id, _ = self._first_field(client, bundle_dir)
        client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "area", "value": "1200 sq ft"})
        field = next(f for f in box_state(client, "sale_deed")["fields"]
                     if f["name"] == "area")
        assert "carpet" in field["value"]


class TestFeedbackLog:
    def test_log_records_the_original_confidence(self, client, bundle_dir: Path):
        """The calibration signal: the outcome alone says nothing about whether the
        confidence was earned."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        box = box_state(client, "sale_deed")
        client.post("/api/accept-field", data={
            "document_id": box["document_id"], "field": box["fields"][0]["name"]})

        log = client.get("/api/feedback").json()
        assert log["decisions"] == 1
        assert log["history"][0]["original_confidence"]
        assert log["by_confidence"]

    def test_correction_rate_is_reported_by_confidence(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]
        client.post("/api/accept-field",
                    data={"document_id": doc_id, "field": "cts_number"})
        client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "9900000"})

        buckets = client.get("/api/feedback").json()["by_confidence"]
        assert any(b.get("correction_rate") is not None for b in buckets.values())

    def test_history_is_append_only(self, client, bundle_dir: Path):
        """Changing your mind must not erase that you decided otherwise first."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]
        client.post("/api/accept-field",
                    data={"document_id": doc_id, "field": "consideration"})
        client.post("/api/correct-field", data={
            "document_id": doc_id, "field": "consideration", "value": "9900000"})

        log = client.get("/api/feedback").json()
        assert log["decisions"] == 1        # current state
        assert len(log["history"]) == 2     # both decisions retained

    def test_reset_clears_feedback(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]
        client.post("/api/accept-field",
                    data={"document_id": doc_id, "field": "consideration"})
        client.post("/api/reset")
        assert client.get("/api/feedback").json()["decisions"] == 0

    def test_unknown_field_is_ignored_not_crashed(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc_id = box_state(client, "sale_deed")["document_id"]
        assert client.post("/api/accept-field", data={
            "document_id": doc_id, "field": "no_such_field"}).is_success
        assert client.get("/api/feedback").json()["decisions"] == 0


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
    """The ADR-0002 control in its conditional form: localhost, OR authenticated."""

    def test_refuses_a_non_loopback_host_without_a_token(self):
        from dmocr.web.auth import AccessControl, check_binding

        with pytest.raises(SystemExit, match="without an access token"):
            check_binding("0.0.0.0", AccessControl())

    def test_refuses_a_hostname_without_a_token(self):
        from dmocr.web.auth import AccessControl, check_binding

        with pytest.raises(SystemExit):
            check_binding("example.com", AccessControl())

    def test_allows_a_non_loopback_host_once_a_token_is_set(self):
        from dmocr.web.auth import AccessControl, check_binding

        check_binding("0.0.0.0", AccessControl("s3cret"))   # must not raise

    def test_loopback_never_needs_a_token(self):
        from dmocr.web.auth import AccessControl, check_binding

        check_binding("127.0.0.1", AccessControl())


class TestAccessControl:
    def test_token_comparison_rejects_a_wrong_token(self):
        from dmocr.web.auth import AccessControl

        access = AccessControl("correct")
        assert access.matches("correct")
        assert not access.matches("wrong")
        assert not access.matches(None)

    def test_disabled_control_matches_nothing(self):
        from dmocr.web.auth import AccessControl

        assert not AccessControl().enabled
        assert not AccessControl().matches("anything")

    def test_generated_tokens_are_unique_and_long(self):
        from dmocr.web.auth import generate_token

        a, b = generate_token(), generate_token()
        assert a != b and len(a) >= 24


@pytest.fixture
def public_client(monkeypatch):
    """A client with token access enabled, as in shared/tunnel mode."""
    from dmocr.web import app as web_app

    session = ReviewSession()
    monkeypatch.setattr(web_app, "session", session)
    monkeypatch.setattr(web_app.access, "token", "test-token")
    monkeypatch.setattr(web_app, "PUBLIC_MODE", True)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        monkeypatch.setattr(web_app.access, "token", None)


class TestTokenAccess:
    def test_request_without_a_token_is_denied(self, public_client):
        res = public_client.get("/", follow_redirects=False)
        assert res.status_code == 401
        assert "Access required" in res.text

    def test_api_without_a_token_is_denied(self, public_client):
        """Middleware, not a per-route dependency - a new endpoint cannot leak by
        omission."""
        assert public_client.get("/api/state").status_code == 401

    def test_static_assets_are_protected_too(self, public_client):
        assert public_client.get("/static/app.js").status_code == 401

    def test_token_in_the_url_sets_a_cookie_and_redirects(self, public_client):
        """The secret stops travelling in the address bar after the first request."""
        res = public_client.get("/?token=test-token", follow_redirects=False)
        assert res.status_code == 303
        assert "token" not in res.headers["location"]
        cookie = res.headers.get("set-cookie", "")
        assert "dmocr_access=" in cookie and "HttpOnly" in cookie

    def test_cookie_grants_subsequent_access(self, public_client):
        public_client.get("/?token=test-token")     # follows the redirect, keeps cookie
        assert public_client.get("/api/state").is_success

    def test_bearer_header_is_accepted(self, public_client):
        res = public_client.get(
            "/api/state", headers={"Authorization": "Bearer test-token"})
        assert res.is_success

    def test_a_wrong_token_is_denied(self, public_client):
        assert public_client.get("/?token=nope", follow_redirects=False).status_code == 401

    def test_healthz_is_open_so_tunnels_can_probe_it(self, public_client):
        res = public_client.get("/healthz")
        assert res.is_success and res.json() == {"ok": True}

    def test_public_mode_shows_the_demo_banner(self, public_client):
        html = public_client.get("/?token=test-token").text
        assert "DEMO INSTANCE" in html
        assert "do not upload real customer documents" in html.lower()

    def test_localhost_mode_shows_no_demo_banner(self, client):
        assert "DEMO INSTANCE" not in client.get("/").text


class TestServiceUnits:
    def test_box_label_covers_every_box(self):
        for doc_type, label, _ in BOXES:
            assert web_service.box_label(doc_type.value) == label
        assert web_service.box_label(OTHER_BOX) == "Other documents"
