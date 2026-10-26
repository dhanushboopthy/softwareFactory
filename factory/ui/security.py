"""Who may talk to the UI, and how.

The UI is a local tool whose POST requests start paid builds that run generated code, so it must
resist other web pages as well as other machines:

* it binds to loopback by default, and only answers requests whose ``Host`` header is its own
  address (this also defeats DNS rebinding);
* every state-changing request needs the per-process CSRF token that only its own forms carry, and a
  same-origin ``Origin`` / ``Sec-Fetch-Site``;
* it can be served beyond loopback only with an explicit flag and an access token (a cookie set by
  a login form);
* pages have no scripts at all, and the content-security-policy forbids them.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from factory.config import FactoryConfig

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MIN_TOKEN_LENGTH = 16
COOKIE = "ui_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Paths reachable without the access token (the login form and the stylesheet it uses).
PUBLIC_PATHS = frozenset({"/login", "/static/ui.css"})
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    # No scripts anywhere (AGENTS.md rule 6); styles only from our own stylesheet.
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; img-src 'self'; "
                                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
}


class UiConfigError(ValueError):
    """The UI was asked to start in a way that is not safe."""


@dataclass(frozen=True)
class UiSettings:
    cfg: FactoryConfig
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = None  # required beyond loopback; optional on it
    extra_hosts: tuple[str, ...] = ()  # more accepted Host headers (name:port), remote use
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @property
    def allowed_hosts(self) -> frozenset[str]:
        names = {f"{h}:{self.port}" for h in ("127.0.0.1", "localhost")} | {f"[::1]:{self.port}"}
        if not self.loopback:
            names.add(f"{self.host}:{self.port}")
        return frozenset(names | set(self.extra_hosts))

    def validate(self) -> None:
        if self.auth_token is not None and len(self.auth_token) < MIN_TOKEN_LENGTH:
            raise UiConfigError(f"FACTORY_UI_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
        if not self.loopback and self.auth_token is None:
            raise UiConfigError(
                f"serving on {self.host} (not loopback) needs FACTORY_UI_TOKEN: anyone who can "
                "reach this server could start builds that run code on this machine")


def tokens_match(supplied: str | None, expected: str) -> bool:
    return supplied is not None and hmac.compare_digest(supplied.encode(), expected.encode())


def _refuse(status: int, message: str) -> Response:
    return PlainTextResponse(message, status_code=status, headers=HEADERS)


def check_origin(request: Request, host: str) -> str | None:
    """Why this state-changing request must be refused, or None if it is same-origin."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
        return "cross-origin request refused"
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return "cross-site request refused"
    return None


def install_security(app: FastAPI, settings: UiSettings) -> None:
    """Host, origin and access-token checks, plus the security headers on every response."""

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]
                    ) -> Response:
        host = request.headers.get("host", "")
        if host not in settings.allowed_hosts:
            return _refuse(400, "Invalid Host header")
        if request.method not in SAFE_METHODS:
            problem = check_origin(request, host)
            if problem:
                return _refuse(403, problem)
        if settings.auth_token and request.url.path not in PUBLIC_PATHS and not tokens_match(
                request.cookies.get(COOKIE), settings.auth_token):
            return _refuse(401, "Not signed in: open /login and enter the access token")
        response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        return response


async def require_csrf(request: Request) -> None:
    """Dependency for every POST route: the form must carry this process's CSRF token."""
    settings: UiSettings = request.app.state.settings
    form = await request.form()
    supplied = form.get("csrf")
    if not isinstance(supplied, str) or not tokens_match(supplied, settings.csrf_token):
        raise HTTPException(status_code=403, detail="Form expired or invalid: reload the page")
