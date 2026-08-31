"""Tests for the System view.

This view exists to make the system's **negative** answers visible — blocked requirements,
unapproved rules, sources that cannot be automated, values that were discarded. Those
facts live in YAML and Markdown, which means they are invisible while you work.

So most of these tests assert that an uncomfortable fact is actually shown.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")

from fastapi.testclient import TestClient  # noqa: E402

from dmocr.web.app import app  # noqa: E402
from dmocr.web.service import ReviewSession  # noqa: E402


@pytest.fixture
def client(monkeypatch):
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
        yield c


def upload(client, box: str, path: Path):
    with path.open("rb") as fh:
        return client.post("/api/upload", data={"box": box},
                           files={"file": (path.name, fh, "application/pdf")})


# =====================================================================================
# Page and navigation
# =====================================================================================


class TestSystemPage:
    def test_page_renders(self, client):
        res = client.get("/system")
        assert res.is_success
        assert "System" in res.text

    def test_page_says_it_is_a_developer_view(self, client):
        assert "developer view" in client.get("/system").text

    def test_both_pages_link_to_each_other(self, client):
        assert 'href="/system"' in client.get("/").text
        assert 'href="/"' in client.get("/system").text


# =====================================================================================
# Rules
# =====================================================================================


class TestRulesPanel:
    def test_lists_every_rule(self, client):
        d = client.get("/api/system/rules").json()
        assert d["total"] == len(d["rules"]) >= 12

    def test_reports_that_no_rule_is_approved(self, client):
        """The most important negative fact on the page: nothing is enforced."""
        d = client.get("/api/system/rules").json()
        assert d["approved"] == 0
        assert "No rule is APPROVED" in (d["note"] or "")

    def test_business_rules_are_distinguished_from_regulatory(self, client):
        rules = {r["rule_id"]: r for r in client.get("/api/system/rules").json()["rules"]}
        assert rules["XDOC_AREA_001"]["regulatory"] is False
        assert rules["OWNERSHIP_001"]["regulatory"] is True

    def test_citations_resolve_to_quoted_requirements(self, client):
        """A citation that does not resolve would mean a rule claiming a basis it lacks."""
        rules = client.get("/api/system/rules").json()["rules"]
        cited = [c for r in rules for c in r["citations"]]
        assert cited
        assert all(c["resolves"] for c in cited)
        assert any(c["quote"] for c in cited)

    def test_rules_show_their_check_and_parameters(self, client):
        rules = {r["rule_id"]: r for r in client.get("/api/system/rules").json()["rules"]}
        ltv = rules["LTV_CAP_001"]
        assert ltv["check"] == "ltv_within_cap"
        assert "slabs" in ltv["params"]

    def test_no_documents_means_no_outcome(self, client):
        assert all(r["outcome"] is None
                   for r in client.get("/api/system/rules").json()["rules"])

    def test_outcome_appears_once_a_document_is_uploaded(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        rules = client.get("/api/system/rules").json()["rules"]
        assert all(r["outcome"] for r in rules)
        assert all(r["outcome"]["advisory"] for r in rules)


# =====================================================================================
# Regulatory
# =====================================================================================


class TestRegulatoryPanel:
    def test_lists_requirements_with_quotes(self, client):
        d = client.get("/api/system/regulatory").json()
        assert d["total"] >= 21
        assert all(r["quote"] for r in d["requirements"])

    def test_blocked_requirements_say_what_blocks_them(self, client):
        """Three are blocked, and a reader must be able to see why without the repo."""
        d = client.get("/api/system/regulatory").json()
        blocked = [r for r in d["requirements"] if not r["rule_ready"]]
        assert d["blocked"] == len(blocked) >= 3
        assert all(r["blocked_because"] for r in blocked)

    def test_secondary_source_blocks_a_requirement(self, client):
        d = client.get("/api/system/regulatory").json()
        reg17 = next(r for r in d["requirements"]
                     if r["id"] == "REQ_REG_17_1_B_COMPULSORY_REGISTRATION")
        assert not reg17["rule_ready"]
        assert "SECONDARY_ONLY" in reg17["blocked_because"]

    def test_legal_review_flag_blocks_a_requirement(self, client):
        d = client.get("/api/system/regulatory").json()
        rera = next(r for r in d["requirements"]
                    if r["id"] == "REQ_RERA_3_2_REGISTRATION_EXEMPTION")
        assert not rera["rule_ready"]
        assert "LEGAL_REVIEW" in rera["blocked_because"]

    def test_negative_findings_are_shown(self, client):
        """What an instrument does NOT say gets silently re-derived if not surfaced."""
        d = client.get("/api/system/regulatory").json()
        assert d["negative_findings"]
        assert any("title" in n["id"].lower() for n in d["negative_findings"])

    def test_source_provenance_is_shown(self, client):
        d = client.get("/api/system/regulatory").json()
        assert d["provenance"]
        assert any(p.get("grade") == "STALE_UNVERIFIED" for p in d["provenance"])


# =====================================================================================
# Pipeline trace
# =====================================================================================


class TestTracePanel:
    def test_empty_before_any_upload(self, client):
        assert client.get("/api/system/trace").json()["documents"] == []

    def test_shows_quality_reading_and_classification(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc = client.get("/api/system/trace").json()["documents"][0]
        assert doc["quality"]["verdict"]
        assert doc["reading"]["text_layer_pages"] >= 1
        assert doc["classification"]["predicted"] == "sale_deed"

    def test_shows_all_classifier_scores_not_just_the_winner(self, client, bundle_dir):
        """A misfire has to be diagnosable, not guessed at."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc = client.get("/api/system/trace").json()["documents"][0]
        assert len(doc["classification"]["scores"]) >= 1
        assert doc["classification"]["signals"]

    def test_shows_per_field_provenance(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        doc = client.get("/api/system/trace").json()["documents"][0]
        fields = doc["extraction"]["by_field"]
        assert fields
        assert all(f["attribute"] and f["source"] for f in fields)

    def test_shows_canonical_claims_and_how_they_resolve(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        upload(client, "property_tax", bundle_dir / "bundle_property_tax.pdf")
        claims = client.get("/api/system/trace").json()["claims"]
        area = next(c for c in claims if c["attribute"] == "property.area")
        assert area["claims"] >= 2
        assert area["determination"] == "MISMATCH"

    def test_shows_resolved_parties_with_their_variants(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        upload(client, "agreement_of_sale", bundle_dir / "bundle_agreement.pdf")
        parties = client.get("/api/system/trace").json()["parties"]
        assert any(len(p["variants"]) > 1 for p in parties)

    def test_pins_the_processing_context(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        ctx = client.get("/api/system/trace").json()["context"]
        assert ctx["pipeline_version"] and ctx["rule_set_version"]
        assert ctx["regulatory_as_of"]


# =====================================================================================
# Verification
# =====================================================================================


class TestVerificationPanel:
    def test_not_planned_before_a_document_exists(self, client):
        assert client.get("/api/system/verification").json()["planned"] is False

    def test_plan_appears_once_there_is_a_property(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        d = client.get("/api/system/verification").json()
        assert d["planned"]
        assert d["plan"]

    def test_shows_exactly_what_would_be_sent(self, client, bundle_dir: Path):
        """An external lookup is an outbound disclosure, so minimisation is visible."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        plan = client.get("/api/system/verification").json()["plan"]
        actionable = [p for p in plan if p["lookup_keys"]]
        assert actionable
        assert all(len(p["lookup_keys"]) <= 1 for p in actionable)

    def test_operator_tasks_carry_full_instructions(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        tasks = client.get("/api/system/verification").json()["tasks"]
        assert tasks
        assert all("do not widen" in t["instruction"] for t in tasks)

    def test_no_check_was_actually_answered(self, client, bundle_dir: Path):
        """No adapters are registered, and the summary must say so rather than imply
        checks passed."""
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        d = client.get("/api/system/verification").json()
        assert d["summary"]["checks_performed"] == 0


# =====================================================================================
# Evaluation and open items
# =====================================================================================


class TestEvaluationPanel:
    def test_reports_when_nothing_has_been_measured(self, client, monkeypatch, tmp_path):
        from dmocr.web import system as sysmod

        monkeypatch.setattr(sysmod, "EVAL_REPORT_PATH", tmp_path / "nope.json")
        d = client.get("/api/system/evaluation").json()
        assert d["available"] is False
        assert "No evaluation" in d["note"]

    def test_reads_a_report_when_present(self, client, monkeypatch, tmp_path):
        import json

        from dmocr.web import system as sysmod

        report = tmp_path / "evaluation.json"
        report.write_text(json.dumps({
            "started_at": "2026-08-25T10:00:00",
            "coverage": {"documents": 4},
            "extraction": {"overall": {"precision": 1.0, "dangerous_error_rate": 0.0},
                           "by_field": {}},
            "classification": {}, "ocr": {}, "findings": {}, "notes": [],
        }), encoding="utf-8")
        monkeypatch.setattr(sysmod, "EVAL_REPORT_PATH", report)

        d = client.get("/api/system/evaluation").json()
        assert d["available"]
        assert d["coverage"]["documents"] == 4
        # The caveat must travel with the numbers, not sit in a doc nobody opens.
        assert "synthetic" in d["caveat"]

    def test_a_second_run_is_refused_while_one_is_going(self, client, monkeypatch):
        from dmocr.web import app as web_app

        monkeypatch.setitem(web_app._eval_state, "running", True)
        res = client.post("/api/system/run-evaluation").json()
        assert res["ok"] is False


class TestOpenItemsPanel:
    def test_parses_items_from_the_markdown(self, client):
        d = client.get("/api/system/open-items").json()
        assert d["total"] > 30
        assert d["open"] and d["closed"]

    def test_closed_items_are_marked(self, client):
        d = client.get("/api/system/open-items").json()
        closed = [i for i in d["items"] if i["closed"]]
        assert closed
        assert any("routing" in i["item"].lower() for i in closed)

    def test_items_are_grouped_by_section(self, client):
        d = client.get("/api/system/open-items").json()
        assert len(d["by_section"]) >= 3
        assert any("Blocked" in s for s in d["by_section"])

    def test_missing_file_degrades_gracefully(self, client, monkeypatch, tmp_path):
        from dmocr.web import system as sysmod

        monkeypatch.setattr(sysmod, "OPEN_ITEMS_PATH", tmp_path / "nope.md")
        d = client.get("/api/system/open-items").json()
        assert d["items"] == []


class TestSystemIsReadOnly:
    def test_viewing_every_panel_changes_nothing(self, client, bundle_dir: Path):
        upload(client, "sale_deed", bundle_dir / "bundle_sale_deed.pdf")
        before = client.get("/api/state").json()
        for panel in ["rules", "regulatory", "trace", "verification",
                      "evaluation", "open-items"]:
            client.get(f"/api/system/{panel}")
        assert client.get("/api/state").json() == before
