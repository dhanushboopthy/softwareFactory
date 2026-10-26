"""The UI's defences: Host, Origin, CSRF, access token, headers and escaping."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from factory.cli import app as cli_app
from factory.config import FactoryConfig
from factory.report.models import RunRecord
from factory.report.writer import write_report
from factory.ui.app import create_app
from factory.ui.environment import Check, Environment
from factory.ui.security import UiConfigError, UiSettings, require_csrf

BASE = "http://127.0.0.1:8765"
TOKEN = "t" * 24
runner = CliRunner()


class StubEnvironment(Environment):
    """Canned checks: the real ones would run `docker info`."""

    def __init__(self, settings: UiSettings, checks: list[Check] | None = None) -> None:
        super().__init__(settings)
        self.canned = checks or [Check("Everything", True, "fine")]

    def checks(self) -> list[Check]:
        return self.canned


def settings_for(tmp_path: Path, **kwargs: Any) -> UiSettings:
    cfg = FactoryConfig(runs_dir=tmp_path / "runs", drafts_dir=tmp_path / "drafts")
    return UiSettings(cfg=cfg, env=kwargs.pop("env", {}), **kwargs)


def client_for(settings: UiSettings, probe: bool = False) -> TestClient:
    application = create_app(settings, StubEnvironment(settings))
    if probe:  # a POST route that exists only to exercise the CSRF dependency
        router = APIRouter(dependencies=[Depends(require_csrf)])

        @router.post("/_probe")
        def accepted() -> PlainTextResponse:
            return PlainTextResponse("accepted")

        application.include_router(router)
    return TestClient(application, base_url=BASE)


@pytest.fixture
def settings(tmp_path: Path) -> UiSettings:
    return settings_for(tmp_path)


# --- Host header (DNS rebinding) ----------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:9999", "127.0.0.1", "localhost",
                                  "127.0.0.1:8765.evil.example", "attacker:8765"])
def test_requests_for_any_other_host_are_refused(settings: UiSettings, host: str) -> None:
    client = client_for(settings)
    for path in ("/", "/static/ui.css", "/nowhere"):
        response = client.get(path, headers={"Host": host})
        assert response.status_code == 400 and response.text == "Invalid Host header", (path, host)


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_the_servers_own_addresses_are_accepted(settings: UiSettings, host: str) -> None:
    assert client_for(settings).get("/", headers={"Host": host}).status_code == 200


# --- CSRF, Origin, Sec-Fetch-Site ---------------------------------------------------------------


def post(client: TestClient, settings: UiSettings, sent: str | None = "right",
         **headers: str) -> Any:
    """POST to the probe route; ``sent`` is the CSRF field: "right", None (absent) or any text."""
    data = {} if sent is None else {"csrf": settings.csrf_token if sent == "right" else sent}
    return client.post("/_probe", data=data, headers=headers)


def test_a_post_needs_this_processs_csrf_token(settings: UiSettings) -> None:
    client = client_for(settings, probe=True)
    assert post(client, settings).text == "accepted"
    for sent in (None, "", "wrong", settings.csrf_token[:-1], settings.csrf_token + "x"):
        response = post(client, settings, sent)
        assert response.status_code == 403, sent
        assert "Form expired or invalid" in response.text


def test_a_token_from_another_process_does_not_work(tmp_path: Path) -> None:
    first, second = settings_for(tmp_path), settings_for(tmp_path)
    assert first.csrf_token != second.csrf_token
    response = client_for(second, probe=True).post(
        "/_probe", data={"csrf": first.csrf_token})
    assert response.status_code == 403


@pytest.mark.parametrize(("headers", "allowed"), [
    ({"Origin": BASE}, True),
    ({"Origin": "http://localhost:8765"}, False),     # a different spelling is a different origin
    ({"Origin": "http://evil.example"}, False),
    ({"Origin": "null"}, False),
    ({"Origin": "https://127.0.0.1:8765"}, True),
    ({"Sec-Fetch-Site": "same-origin"}, True),
    ({"Sec-Fetch-Site": "none"}, True),
    ({"Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "same-site"}, False),
    ({"Origin": BASE, "Sec-Fetch-Site": "cross-site"}, False),
    ({}, True),      # no browser headers at all (curl, tests): the CSRF token is what counts
])
def test_cross_origin_posts_are_refused_even_with_the_token(
        settings: UiSettings, headers: dict[str, str], allowed: bool) -> None:
    response = post(client_for(settings, probe=True), settings, **headers)
    assert (response.status_code == 200) is allowed, (headers, response.status_code)
    if not allowed:
        assert response.status_code == 403


def test_a_refused_post_never_reaches_its_route(settings: UiSettings) -> None:
    reached: list[str] = []
    application = create_app(settings, StubEnvironment(settings))
    router = APIRouter(dependencies=[Depends(require_csrf)])

    @router.post("/_probe")
    def route() -> PlainTextResponse:
        reached.append("yes")
        return PlainTextResponse("accepted")

    application.include_router(router)
    client = TestClient(application, base_url=BASE)
    client.post("/_probe", data={"csrf": "wrong"})
    client.post("/_probe", data={"csrf": settings.csrf_token}, headers={"Origin": "http://x.y"})
    assert reached == []


# --- headers ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/static/ui.css", "/nowhere", "/login"])
def test_every_response_carries_the_security_headers(settings: UiSettings, path: str) -> None:
    response = client_for(settings).get(path)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "script" not in policy and "unsafe-inline" not in policy and "unsafe-eval" not in policy


def test_the_api_docs_are_not_exposed(settings: UiSettings) -> None:
    client = client_for(settings)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_pages_have_no_scripts_and_no_inline_styles(settings: UiSettings) -> None:
    html = client_for(settings).get("/").text
    for forbidden in ("<script", "onclick", "onload", "javascript:", "style="):
        assert forbidden not in html, forbidden
    assert 'href="/static/ui.css"' in html


def test_the_stylesheet_is_served_with_the_right_type(settings: UiSettings) -> None:
    response = client_for(settings).get("/static/ui.css")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/css")
    assert "@import" not in response.text and "url(" not in response.text  # nothing external


# --- escaping -----------------------------------------------------------------------------------


def test_hostile_text_from_a_run_is_escaped(settings: UiSettings) -> None:
    record = RunRecord(run_id="r1", spec_name="<script>alert(1)</script>", spec_path="/x",
                       spec_sha256="h", provider="p", model="m", status="failed",
                       status_reason='"><img src=x onerror=alert(1)>')
    write_report(record, settings.cfg.runs_dir / "r1")
    html = client_for(settings).get("/").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script" not in html and "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html or "&#34;&gt;&lt;img" in html


def test_directories_that_are_not_valid_run_ids_never_appear(settings: UiSettings) -> None:
    runs = settings.cfg.runs_dir
    for name in ("<b>x</b>", ".hidden", "a b"):
        record = RunRecord(run_id="x", spec_name="x", spec_path="/x", spec_sha256="h",
                           provider="p", model="m")
        write_report(record, runs / name)
    html = client_for(settings).get("/").text
    assert "No runs yet" in html and "<b>x</b>" not in html and "hidden" not in html


# --- starting the UI beyond this machine --------------------------------------------------------


def test_serving_beyond_loopback_needs_an_access_token(tmp_path: Path) -> None:
    with pytest.raises(UiConfigError, match="FACTORY_UI_TOKEN"):
        settings_for(tmp_path, host="0.0.0.0").validate()  # noqa: S104 - the case under test
    settings_for(tmp_path, host="0.0.0.0", auth_token=TOKEN).validate()  # noqa: S104, S106


def test_a_short_token_is_refused_everywhere(tmp_path: Path) -> None:
    for host in ("127.0.0.1", "0.0.0.0"):  # noqa: S104
        with pytest.raises(UiConfigError, match="at least 16"):
            settings_for(tmp_path, host=host, auth_token="short").validate()  # noqa: S106


def test_create_app_refuses_an_unsafe_configuration(tmp_path: Path) -> None:
    with pytest.raises(UiConfigError):
        create_app(settings_for(tmp_path, host="192.168.1.5"))


def test_the_accepted_hosts(tmp_path: Path) -> None:
    local = settings_for(tmp_path, port=9000)
    assert local.allowed_hosts == {"127.0.0.1:9000", "localhost:9000", "[::1]:9000"}
    remote = settings_for(tmp_path, host="10.0.0.5", port=9000, auth_token=TOKEN,  # noqa: S106
                          extra_hosts=("factory.internal:9000",))
    assert {"10.0.0.5:9000", "factory.internal:9000", "127.0.0.1:9000"} <= remote.allowed_hosts


def test_the_token_is_not_in_the_settings_repr(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, auth_token=TOKEN)
    assert settings.csrf_token not in repr(settings)


# --- the access token ---------------------------------------------------------------------------


@pytest.fixture
def protected(tmp_path: Path) -> UiSettings:
    return settings_for(tmp_path, auth_token=TOKEN)


def test_without_the_token_nothing_but_the_login_form_is_reachable(protected: UiSettings) -> None:
    client = client_for(protected)
    for path in ("/", "/runs/x", "/nowhere"):
        response = client.get(path)
        assert response.status_code == 401 and "Not signed in" in response.text
    assert client.get("/login").status_code == 200
    assert client.get("/static/ui.css").status_code == 200


def test_the_login_form_carries_the_csrf_token(protected: UiSettings) -> None:
    html = client_for(protected).get("/login").text
    assert f'name="csrf" value="{protected.csrf_token}"' in html
    assert 'type="password"' in html and TOKEN not in html


def test_signing_in_sets_a_strict_http_only_cookie(protected: UiSettings) -> None:
    client = client_for(protected)
    response = client.post("/login", data={"csrf": protected.csrf_token, "token": TOKEN},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert client.get("/").status_code == 200  # the client kept the cookie


@pytest.mark.parametrize("token", ["", "wrong", TOKEN[:-1], TOKEN + "x"])
def test_a_wrong_token_does_not_sign_in(protected: UiSettings, token: str) -> None:
    client = client_for(protected)
    response = client.post("/login", data={"csrf": protected.csrf_token, "token": token})
    assert response.status_code == 401 and "not right" in response.text
    assert "set-cookie" not in response.headers
    assert client.get("/").status_code == 401


def test_signing_in_also_needs_the_csrf_token(protected: UiSettings) -> None:
    response = client_for(protected).post("/login", data={"token": TOKEN})
    assert response.status_code == 403


def test_a_forged_cookie_does_not_work(protected: UiSettings) -> None:
    client = client_for(protected)
    client.cookies.set("ui_token", "forged")
    assert client.get("/").status_code == 401


def test_the_login_page_is_pointless_without_a_token_configured(settings: UiSettings) -> None:
    response = client_for(settings).get("/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


# --- the command --------------------------------------------------------------------------------


def test_the_ui_command_refuses_to_leave_loopback_without_the_flag() -> None:
    result = runner.invoke(cli_app, ["ui", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 2 and "--allow-remote" in result.output


def test_the_ui_command_needs_a_token_to_leave_loopback() -> None:
    result = runner.invoke(cli_app, ["ui", "--host", "0.0.0.0", "--allow-remote"],  # noqa: S104
                           env={"FACTORY_UI_TOKEN": ""})
    assert result.exit_code == 2 and "FACTORY_UI_TOKEN" in result.output


def test_the_ui_command_serves_on_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    served: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kw: served.update(kw))
    result = runner.invoke(cli_app, ["ui", "--port", "9123"])
    assert result.exit_code == 0, result.output
    assert served == {"host": "127.0.0.1", "port": 9123, "log_level": "warning"}
    assert "http://127.0.0.1:9123/" in result.output
