"""Review UI.

    uvicorn dmocr.web.app:app --host 127.0.0.1 --port 8000

BINDS TO LOCALHOST ONLY. There is no authentication (ADR-0002), so an unauthenticated
service handling collateral documents must not be reachable from an untrusted network.
The compensating control is the network boundary; `serve()` enforces it and the app warns
if it is started otherwise.

Uploads are processed in a background thread and the page polls, because OCR runs at
roughly four seconds a page on CPU and a synchronous POST would hang the browser on a
forty-page deed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import system
from ..model.findings import Disposition
from .auth import AccessControl, AccessMiddleware, check_binding
from .render import crop_evidence
from .service import BOXES, OTHER_BOX, ReviewSession, box_label

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Collateral Document Review", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

session = ReviewSession()

#: Set by serve(). Empty in localhost mode, which is the default.
access = AccessControl()
app.add_middleware(AccessMiddleware, access=access)

#: True when reachable beyond loopback. Drives the demo banner - a shared instance must
#: say, on every page, that it is not for real customer documents.
PUBLIC_MODE = False


# =====================================================================================
# Pages
# =====================================================================================


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "boxes": session.boxes(),
        "rules_version": session.rule_set.version if session.rule_set else None,
        "public_mode": PUBLIC_MODE,
    })


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    """Developer view: what the system is doing, and what it is not doing yet."""
    return templates.TemplateResponse(request, "system.html", {
        "public_mode": PUBLIC_MODE,
        "rules_version": session.rule_set.version if session.rule_set else None,
    })


@app.get("/healthz")
def healthz():
    """Open by design, so a tunnel health check does not need the token."""
    return {"ok": True}


# =====================================================================================
# System view
# =====================================================================================


@app.get("/api/system/rules")
def system_rules():
    return system.rules_view(session)


@app.get("/api/system/regulatory")
def system_regulatory():
    return system.regulatory_view()


@app.get("/api/system/trace")
def system_trace():
    return system.trace_view(session)


@app.get("/api/system/verification")
def system_verification():
    return system.verification_view(session)


@app.get("/api/system/evaluation")
def system_evaluation():
    return {**system.evaluation_view(), "running": _eval_state["running"],
            "last_error": _eval_state["error"]}


@app.get("/api/system/open-items")
def system_open_items():
    return system.open_items_view()


#: Guarded so two clicks cannot start two runs over the same output directory.
_eval_state: dict = {"running": False, "error": None}


def _run_evaluation() -> None:
    from ..eval import EvaluationRunner, load_corpus, write_report

    try:
        corpus = load_corpus(system.REPO_ROOT / "eval/groundtruth")
        documents = system.REPO_ROOT / "fixtures"
        if not documents.is_dir():
            raise FileNotFoundError(
                "fixtures/ not found - run `python tools/make_fixtures.py fixtures` first")
        result = EvaluationRunner().run(corpus, documents)
        write_report(result, system.REPO_ROOT / "eval-output")
        _eval_state["error"] = None
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not swallowed
        log.exception("evaluation failed")
        _eval_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _eval_state["running"] = False


@app.post("/api/system/run-evaluation")
def run_evaluation():
    """Kick off the harness in the background. It takes tens of seconds."""
    if _eval_state["running"]:
        return {"ok": False, "error": "An evaluation is already running."}
    _eval_state["running"] = True
    _eval_state["error"] = None
    threading.Thread(target=_run_evaluation, daemon=True).start()
    return {"ok": True}


# =====================================================================================
# API
# =====================================================================================


@app.post("/api/upload")
async def upload(box: str = Form(...), file: UploadFile = None):
    valid = {t.value for t, _, _ in BOXES} | {OTHER_BOX}
    if box not in valid:
        raise HTTPException(400, f"unknown box {box!r}")
    if file is None or not file.filename:
        raise HTTPException(400, "no file")

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")

    document_id = session.accept_upload(box, file.filename, data)
    # Background thread, not a blocking call: OCR is slow enough that the browser would
    # time out on a long document.
    threading.Thread(
        target=session.process, args=(document_id,), daemon=True).start()
    return {"document_id": document_id, "status": "processing"}


@app.get("/api/state")
def state():
    """Full board state. The page polls this while anything is processing."""
    boxes = session.boxes()
    return JSONResponse({
        "boxes": [_box_json(b) for b in boxes],
        "processing": any(b.status.value == "processing" for b in boxes),
        "findings": [_finding_json(f) for f in session.findings],
        "summary": session.finding_summary,
        "notes": session.case_notes,
        "documents_present": sum(1 for b in boxes if b.document_id),
    })


@app.post("/api/confirm")
def confirm(document_id: str = Form(...)):
    session.confirm_type(document_id)
    return {"ok": True}


@app.post("/api/move")
def move(document_id: str = Form(...), target: str = Form(...)):
    session.move_document(document_id, target)
    return {"ok": True}


@app.post("/api/remove")
def remove(document_id: str = Form(...)):
    session.remove(document_id)
    return {"ok": True}


@app.post("/api/accept-field")
def accept_field(document_id: str = Form(...), field: str = Form(...)):
    session.accept_field(document_id, field)
    return {"ok": True}


@app.post("/api/correct-field")
def correct_field(document_id: str = Form(...), field: str = Form(...),
                  value: str = Form(...)):
    """Replace an extracted value with the reviewer's.

    A correction that cannot be read is refused with the reason rather than stored as a
    guess - putting a wrong value into the case under a human's authority would be worse
    than the extraction error being corrected.
    """
    from .feedback import CorrectionError

    try:
        session.correct_field(document_id, field, value)
    except CorrectionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True}


@app.get("/api/feedback")
def feedback():
    """Reviewer decisions, and the correction rate by claimed confidence.

    The calibration question in one place: when the system said HIGH, how often was it
    wrong?
    """
    log = session.feedback
    return {
        "decisions": len(log),
        "corrections": len(log.corrections()),
        "by_confidence": log.calibration_summary(),
        "history": [
            {"document_id": e.document_id, "field": e.field_name,
             "action": e.action.value, "original_confidence": e.original_confidence,
             "at": e.at.isoformat(timespec="seconds")}
            for e in log.history
        ],
    }


@app.post("/api/reset")
def reset():
    session.reset()
    return {"ok": True}


@app.get("/evidence/{document_id}/{page}")
def evidence(document_id: str, page: int, x0: float = None, y0: float = None,
             x1: float = None, y1: float = None):
    """The exact region a value was read from, as a PNG."""
    ctx = session.context(document_id)
    if ctx is None:
        raise HTTPException(404, "unknown document")

    bbox = None
    if None not in (x0, y0, x1, y1):
        from ..model.provenance import BoundingBox

        try:
            bbox = BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)
        except Exception:
            bbox = None

    png = crop_evidence(ctx.data, page, bbox)
    if png is None:
        raise HTTPException(404, "could not render this page")
    return Response(
        content=png, media_type="image/png",
        # Content-addressed by document and region, so it is safe to cache locally.
        headers={"Cache-Control": "private, max-age=300"},
    )


# =====================================================================================
# Serialisation
# =====================================================================================


def _box_json(box) -> dict:
    data = asdict(box)
    data["status"] = box.status.value
    data["stages"] = [
        {"key": s.key, "label": s.label, "status": s.status.value, "detail": s.detail}
        for s in box.stages
    ]
    return data


_DISPOSITION_ORDER = {
    Disposition.BLOCKER: 0,
    Disposition.REVIEW_REQUIRED: 1,
    Disposition.INFORMATIONAL: 2,
    Disposition.CLEARED: 3,
    Disposition.NOT_APPLICABLE: 4,
}


def _finding_json(f) -> dict:
    return {
        "rule_id": f.rule_id,
        "title": f.title,
        "severity": f.severity.value,
        "disposition": f.disposition.value,
        "determination": f.determination.value,
        "message": f.message,
        "recommended_action": f.recommended_action,
        "category": f.category,
        # Empty citations mean a BUSINESS rule. The UI must not imply regulatory backing
        # a rule does not have.
        "regulatory": f.is_regulatory,
        "citations": f.citations,
        # Every rule is DRAFT until legal sign-off, so nothing here is enforced.
        "advisory": f.advisory_only,
        "evidence_note": f.evidence.note,
        "order": _DISPOSITION_ORDER.get(f.disposition, 9),
    }


def configure_access(token: str | None) -> None:
    """Install the shared token and switch on public mode."""
    global PUBLIC_MODE

    access.token = token or None
    PUBLIC_MODE = access.enabled


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    token: str | None = None,
) -> None:
    """Run the UI.

    Binds to loopback freely. Binds anywhere else ONLY with a token - the conditional
    form of the ADR-0002 control: localhost, or authenticated, never neither.
    """
    import uvicorn

    configure_access(token)
    check_binding(host, access)
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = ["app", "serve", "configure_access", "box_label"]
