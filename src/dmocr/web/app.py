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

from ..model.findings import Disposition
from .render import crop_evidence
from .service import BOXES, OTHER_BOX, ReviewSession, box_label

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Collateral Document Review", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

session = ReviewSession()


# =====================================================================================
# Pages
# =====================================================================================


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "boxes": session.boxes(),
        "rules_version": session.rule_set.version if session.rule_set else None,
    })


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


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the UI. Refuses to bind anywhere but loopback."""
    import ipaddress

    import uvicorn

    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError:
        raise SystemExit(
            f"refusing to bind to {host!r}. There is no authentication (ADR-0002), so "
            f"this service must not be reachable from an untrusted network. Use "
            f"127.0.0.1 and tunnel if you need remote access."
        ) from None
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = ["app", "serve", "box_label"]
