"""Login, CSRF protection and security headers.

- Login is optional: set JOBSCOUT_PASSWORD (or JOBSCOUT_PASSWORD_FILE) and every page except
  /login, /healthz and /static needs a signed session cookie. Without it the app trusts its
  network, so bind it to localhost or put it behind Tailscale / an authenticating proxy.
- CSRF: state-changing requests must come from this site (Sec-Fetch-Site, falling back to
  Origin/Referer). Without this any page you visit could POST to the app - e.g. point Home
  Assistant alerts at its own server and press "Send test" to receive HA_TOKEN.
"""

from __future__ import annotations

import hmac
import os
import secrets
from urllib.parse import quote, urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from .credentials import env
from .db import DATA_DIR

PUBLIC_PATHS = ("/login", "/healthz", "/static/")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def password() -> str:
    return env("JOBSCOUT_PASSWORD")


def password_ok(candidate: str) -> bool:
    expected = password()
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


def session_secret() -> str:
    """JOBSCOUT_SECRET_KEY, else a random key generated once and kept in the data directory."""
    if key := env("JOBSCOUT_SECRET_KEY"):
        return key
    path = DATA_DIR / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


def is_local_host(request: Request) -> bool:
    return (request.url.hostname or "") in ("localhost", "127.0.0.1", "::1")


def _same_origin(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return source is None  # non-browser clients (curl, tests) send neither and carry no cookies
    return urlsplit(source).netloc == request.headers.get("host", "")


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in SAFE_METHODS and not _same_origin(request):
            return PlainTextResponse("Cross-site request blocked", status_code=403)

        path = request.url.path
        if password() and not path.startswith(PUBLIC_PATHS) and not request.session.get("auth"):
            target = "/login?next=" + quote(path + (f"?{request.url.query}" if request.url.query else ""))
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": target})
            return RedirectResponse(target, status_code=303)

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response


def safe_next(target: str) -> str:
    """Only redirect back to a path on this site."""
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) else "/"
