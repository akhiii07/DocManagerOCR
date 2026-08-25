"""Access control for the review UI.

ADR-0002 skipped authentication for the MVP, and the compensating control was that the
service binds to localhost only. That control works right up until someone needs to share
the UI — at which point the tempting move is to remove the loopback guard and expose an
unauthenticated document-upload endpoint to the internet.

So the guard is made **conditional** rather than removed:

    non-loopback binding is allowed ONLY when a token is configured

The token is a shared secret in the URL — a capability URL. That is appropriate for a
demonstration and NOT for real collateral documents:

* a token in a URL leaks through browser history, referrer headers and shoulder-surfing
* one token means one identity for everyone, so the audit ledger cannot attribute actions
* there is no revocation beyond restarting with a new token

Which is why public mode also forces the demo banner. Real documents need real
authentication (OPEN-ITEMS 13) and a private deployment, not a tunnel.
"""

from __future__ import annotations

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, RedirectResponse

log = logging.getLogger(__name__)

COOKIE_NAME = "dmocr_access"
TOKEN_PARAM = "token"

#: Reachable without a token, so a tunnel health check does not need the secret.
OPEN_PATHS = {"/healthz"}


def generate_token() -> str:
    """A URL-safe token with enough entropy to resist guessing."""
    return secrets.token_urlsafe(24)


class AccessControl:
    """Holds the shared token, or nothing at all in localhost mode."""

    def __init__(self, token: str | None = None):
        self.token = token or None

    @property
    def enabled(self) -> bool:
        return self.token is not None

    def matches(self, candidate: str | None) -> bool:
        if not self.enabled or not candidate:
            return False
        # Constant-time: a naive == leaks the token a character at a time under timing
        # analysis.
        return secrets.compare_digest(candidate, self.token or "")


_DENIED_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Access required</title>
<style>body{font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;color:#1c2530;
background:#f6f7f9;display:flex;align-items:center;justify-content:center;height:100vh;
margin:0}div{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:24px 28px;
max-width:460px}h1{font-size:17px;margin:0 0 8px}p{color:#667085;font-size:13px}</style>
</head><body><div>
<h1>Access required</h1>
<p>This link needs the access token it was shared with. Ask whoever sent it for the
full URL.</p>
</div></body></html>"""


class AccessMiddleware(BaseHTTPMiddleware):
    """Gates every request when a token is configured.

    Applied as middleware rather than a per-route dependency so that static assets and
    any future route are covered by default. A new endpoint should not be able to become
    public by omission.
    """

    def __init__(self, app, access: AccessControl):
        super().__init__(app)
        self.access = access

    async def dispatch(self, request, call_next):
        if not self.access.enabled or request.url.path in OPEN_PATHS:
            return await call_next(request)

        if self.access.matches(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.startswith("Bearer ") and self.access.matches(header[7:].strip()):
            return await call_next(request)

        supplied = request.query_params.get(TOKEN_PARAM)
        if self.access.matches(supplied):
            # Swap the token for a cookie and redirect to a clean URL, so the secret
            # stops travelling in the address bar and referrer headers.
            clean = request.url.remove_query_params(TOKEN_PARAM)
            response = RedirectResponse(str(clean), status_code=303)
            response.set_cookie(
                COOKIE_NAME,
                self.access.token or "",
                httponly=True,
                samesite="lax",
                # The tunnel terminates TLS, so the cookie should not travel in clear.
                secure=request.url.scheme == "https",
                max_age=8 * 3600,
            )
            return response

        log.warning("denied %s %s from %s", request.method, request.url.path,
                    request.client.host if request.client else "?")
        return HTMLResponse(_DENIED_HTML, status_code=401)


def check_binding(host: str, access: AccessControl) -> None:
    """Refuse a non-loopback bind unless a token is configured.

    The conditional form of the ADR-0002 control: localhost, OR authenticated. Never
    neither.
    """
    import ipaddress

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False

    if loopback:
        return
    if not access.enabled:
        raise SystemExit(
            f"refusing to bind to {host!r} without an access token.\n"
            f"There is no user authentication (ADR-0002), so an exposed instance would "
            f"be an open document-upload endpoint.\n"
            f"Use --public (which generates a token), or bind to 127.0.0.1."
        )
