"""Upload safety checks.

A collateral bundle is untrusted input arriving from outside the organisation, and PDF is
a format with executable and network-capable features. Before anything parses a document
for content, we check what capabilities it declares.

**Known limitation, stated plainly.** This scans the raw bytes for capability names. A PDF
can place object definitions inside compressed object streams, where these names will not
appear in plaintext, so a determined adversary can evade this check. It is a cheap first
filter that catches malformed and opportunistically malicious files — it is NOT a security
boundary. The real boundaries are: rendering in a sandboxed process with no network
egress, and never executing anything a document declares. Those are deployment concerns
recorded in the data-handling policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class SafetyVerdict(StrEnum):
    SAFE = "SAFE"
    #: Capabilities that are unusual but not executable. Process with a note.
    SUSPICIOUS = "SUSPICIOUS"
    #: Active or network-capable content. Refuse.
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class SafetyFinding:
    code: str
    detail: str
    blocking: bool


@dataclass(frozen=True)
class SafetyReport:
    verdict: SafetyVerdict
    findings: list[SafetyFinding]
    declared_type: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.verdict is SafetyVerdict.BLOCKED


#: name -> (code, human explanation, blocking)
_PDF_CAPABILITIES: dict[bytes, tuple[str, str, bool]] = {
    b"/JavaScript": ("PDF_JAVASCRIPT", "Document declares embedded JavaScript.", True),
    b"/JS": ("PDF_JAVASCRIPT", "Document declares a JavaScript action.", True),
    b"/Launch": ("PDF_LAUNCH", "Document declares a Launch action (external program).", True),
    b"/EmbeddedFile": ("PDF_EMBEDDED_FILE", "Document carries an embedded file attachment.", True),
    b"/RichMedia": ("PDF_RICH_MEDIA", "Document declares rich media (Flash/video).", True),
    b"/GoToR": ("PDF_REMOTE_GOTO", "Document declares a remote go-to action.", True),
    b"/SubmitForm": ("PDF_SUBMIT_FORM", "Document declares a form submission action.", True),
    b"/OpenAction": ("PDF_OPEN_ACTION", "Document declares an action on open.", False),
    b"/AA": ("PDF_ADDITIONAL_ACTIONS", "Document declares additional (event) actions.", False),
    b"/XFA": ("PDF_XFA", "Document uses XFA forms.", False),
}

_MAGIC: dict[str, bytes] = {
    "pdf": b"%PDF-",
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "tiff_le": b"II*\x00",
    "tiff_be": b"MM\x00*",
}


def sniff_type(data: bytes) -> str | None:
    """Identify the format from magic bytes, ignoring the filename.

    A filename is an assertion by the uploader. The bytes are the fact. Trusting the
    extension is how a mislabelled or disguised file reaches a parser that does not
    expect it.
    """
    for name, magic in _MAGIC.items():
        if data.startswith(magic):
            return "tiff" if name.startswith("tiff") else name
    return None


def scan(data: bytes, *, declared_name: str | None = None) -> SafetyReport:
    """Inspect an upload before anything parses it for content."""
    findings: list[SafetyFinding] = []

    kind = sniff_type(data)
    if kind is None:
        findings.append(SafetyFinding(
            "UNRECOGNISED_FORMAT",
            "File does not begin with a recognised PDF or image signature.",
            blocking=True,
        ))
        return SafetyReport(SafetyVerdict.BLOCKED, findings, None)

    if declared_name:
        ext = declared_name.rsplit(".", 1)[-1].lower() if "." in declared_name else ""
        expected = {"pdf": {"pdf"}, "png": {"png"}, "jpeg": {"jpg", "jpeg"},
                    "tiff": {"tif", "tiff"}}.get(kind, set())
        if ext and expected and ext not in expected:
            findings.append(SafetyFinding(
                "EXTENSION_MISMATCH",
                f"File is {kind} but named .{ext}.",
                blocking=False,
            ))

    if kind == "pdf":
        seen: set[str] = set()
        for token, (code, detail, blocking) in _PDF_CAPABILITIES.items():
            if token in data and code not in seen:
                seen.add(code)
                findings.append(SafetyFinding(code, detail, blocking))

        if not re.search(rb"%%EOF\s*$", data[-2048:] if len(data) > 2048 else data):
            findings.append(SafetyFinding(
                "PDF_TRUNCATED",
                "No %%EOF marker near end of file; the upload may be truncated.",
                blocking=False,
            ))

    if any(f.blocking for f in findings):
        verdict = SafetyVerdict.BLOCKED
    elif findings:
        verdict = SafetyVerdict.SUSPICIOUS
    else:
        verdict = SafetyVerdict.SAFE
    return SafetyReport(verdict, findings, kind)
