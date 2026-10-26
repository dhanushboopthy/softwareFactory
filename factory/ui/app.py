"""The UI application: routes, rendering and error pages.

Pages are server-rendered Jinja2 (autoescape on) with one stylesheet and no scripts. Progress is
read from the ``run.json`` files the pipeline already writes after every station and iteration;
the UI keeps no state of its own about runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment as JinjaEnvironment
from jinja2 import PackageLoader, StrictUndefined
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile  # what request.form() returns (not FastAPI's)
from starlette.exceptions import HTTPException as StarletteHTTPException

from factory.export import client_archive, project_archive
from factory.report.runs import list_summaries, read_summary, run_dir_for, valid_run_id
from factory.report.writer import load_record
from factory.stations import spec_writer
from factory.ui import files
from factory.ui.drafts import (
    DraftMeta,
    DraftRunner,
    DraftStore,
    safe_name,
    set_approved,
    text_hash,
)
from factory.ui.environment import Environment, check_provider
from factory.ui.jobs import BuildBusy, BuildRunner, unique_run_id, validate_for_build
from factory.ui.security import (
    COOKIE,
    UiSettings,
    install_security,
    require_csrf,
    tokens_match,
)
from factory.ui.views import (
    REFRESH_S,
    STATUS_CLASSES,
    budget_bars,
    check_chip,
    draft_summary,
    fmt_cost,
    fmt_duration,
    fmt_time,
    refresh_seconds,
    run_row,
    station_chip,
)

STYLESHEET = Path(__file__).parent / "static" / "ui.css"

_jinja = JinjaEnvironment(
    loader=PackageLoader("factory", "ui/templates"),
    autoescape=True,  # every value is escaped; run names and agent output are untrusted text
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_REASONS = {
    404: ("Page not found", "There is nothing at this address."),
    405: ("Not allowed", "This address does not accept that kind of request."),
}


def render(request: Request, template: str, status: int = 200, **context: Any) -> HTMLResponse:
    settings: UiSettings = request.app.state.settings
    context.setdefault("refresh", None)
    context.setdefault("login", False)
    html = _jinja.get_template(template).render(csrf=settings.csrf_token, **context)
    return HTMLResponse(html, status_code=status)


def message(request: Request, status: int, heading: str, text: str, **extra: Any) -> HTMLResponse:
    return render(request, "message.html.j2", status, heading=heading, message=text, **extra)


def create_app(settings: UiSettings, environment: Environment | None = None,
               runner: BuildRunner | None = None,
               draft_runner: DraftRunner | None = None) -> FastAPI:
    settings.validate()
    build_runner = runner or BuildRunner(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        build_runner.shutdown()  # a build must not outlive the server that started it

    app = FastAPI(title="API Factory", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.settings = settings
    app.state.environment = environment or Environment(settings)
    app.state.runner = build_runner
    draft_store = DraftStore(settings.cfg.drafts_dir / "_drafts")
    draft_runner = draft_runner or DraftRunner(draft_store, settings.cfg, settings.env)
    app.state.drafts = draft_store
    install_security(app, settings)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        summaries = list_summaries(settings.cfg.runs_dir)
        current = build_runner.current()
        drafts = draft_store.all_drafts()[:8]
        drafting = any(d.status == "drafting" for d in drafts)
        return render(request, "dashboard.html.j2", checks=app.state.environment.checks(),
                      rows=[run_row(s) for s in summaries], current=current, drafts=drafts,
                      refresh=REFRESH_S if current or drafting else refresh_seconds(summaries))

    @app.get("/static/ui.css")
    def stylesheet() -> Response:
        return Response(STYLESHEET.read_bytes(), media_type="text/css")

    def run_dir(run_id: str) -> Path:
        """The directory of an existing run; anything else is a plain 404."""
        try:
            path = run_dir_for(settings.cfg.runs_dir, run_id)
        except ValueError:
            raise HTTPException(status_code=404) from None
        if not (path / "run.json").is_file():
            raise HTTPException(status_code=404)
        return path

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str) -> Response:
        job = build_runner.job_for(run_id)
        try:
            directory = run_dir(run_id)
        except HTTPException:
            if job is None:
                raise
            # Started from here, but the build has not written its first report yet (or died
            # before it could): show what it printed instead of a 404.
            return render(request, "starting.html.j2", run_id=run_id, running=job.running,
                          returncode=job.returncode, console=build_runner.log_tail(run_id),
                          refresh=REFRESH_S if job.running else None)
        try:
            record = load_record(directory)
        except (ValidationError, ValueError, OSError):
            return message(request, 500, "Run unreadable",
                           "This run's run.json cannot be read; it may be damaged.")
        summary = read_summary(directory)
        now = datetime.now(UTC)
        stalled = bool(summary and summary.stale(now))
        active = record.status == "running" and not stalled
        project = directory / "project"
        return render(
            request, "run.html.j2", r=record, active=active, stalled=stalled,
            status_label="stalled" if stalled else record.status,
            status_class="warn" if stalled else STATUS_CLASSES.get(record.status, ""),
            started=fmt_time(record.started_at),
            duration=fmt_duration(record.started_at, record.ended_at, now),
            cost=fmt_cost(record.usage.cost_usd), bars=budget_bars(record),
            stations=[(s, *station_chip(s, active, stalled)) for s in record.stations],
            iteration_checks=[[(c, *check_chip(c)) for c in it.checks]
                              for it in record.iterations],
            has_project=project.is_dir() and not project.is_symlink(),
            has_client=(project / "client").is_dir() and not (project / "client").is_symlink(),
            has_openapi=(project / "openapi.yaml").is_file(),
            job=job, force_offered=bool(job and job.force_offered()),
            console=build_runner.log_tail(run_id),
            refresh=REFRESH_S if (active or (job and job.running)) else None)

    @app.get("/builds/new", response_class=HTMLResponse)
    def new_build(request: Request) -> Response:
        return render(request, "builds_new.html.j2", **new_build_context())

    def new_build_context(problems: list[str] | None = None, no_llm: bool = False
                          ) -> dict[str, Any]:
        checks = app.state.environment.checks()
        return {"current": build_runner.current(), "problems": problems or [], "no_llm": no_llm,
                "blockers": [c for c in checks if c.ok is False and c.name != "LLM provider"]}

    @app.post("/builds", dependencies=[Depends(require_csrf)])
    async def start_build(request: Request) -> Response:
        form = await request.form()
        upload = form.get("entities")
        no_llm = form.get("no_llm") is not None
        if not isinstance(upload, UploadFile):
            return message(request, 400, "No file", "Choose an entities file to build.")
        raw = await upload.read(settings.cfg.context_max_total_chars + 1)
        if len(raw) > settings.cfg.context_max_total_chars:
            return message(request, 413, "File too large",
                           f"An entities file is at most {settings.cfg.context_max_total_chars:,} "
                           "characters.")
        return await run_in_threadpool(begin_build, request, raw, upload.filename or "", no_llm)

    def begin_build(request: Request, raw: bytes, filename: str, no_llm: bool) -> Response:
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            return message(request, 400, "Not a text file", "The file is not UTF-8 text.")
        doc, problems = validate_for_build(text)
        if not no_llm and not problems and not check_provider(settings).ok:
            problems = ["No LLM provider is configured, so the agents cannot run. Configure one "
                        "in .env and restart the UI, or tick “generate and check only”."]
        if doc is None or problems:
            return render(request, "builds_new.html.j2", 422, **new_build_context(problems, no_llm))
        run_id = unique_run_id(settings, Path(filename or "entities.yaml"), doc.app)
        source = settings.cfg.drafts_dir / run_id / "entities.yaml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text)
        try:
            build_runner.start(source, run_id, no_llm)
        except BuildBusy as busy:
            return message(request, 409, "A build is already running",
                           f"Build {busy.run_id} is still running; only one build runs at a time.")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    # --- drafts: write or upload an entities file, review it, approve it, build it -----------

    def draft_or_404(draft_id: str) -> DraftMeta:
        meta = draft_store.get(draft_id)
        if meta is None:
            raise HTTPException(status_code=404)
        return meta

    def draft_context(meta: DraftMeta, problems: list[str] | None = None,
                      notice: str = "") -> dict[str, Any]:
        text = draft_store.read_text(meta.id) if draft_store.has_text(meta.id) else ""
        doc, found = spec_writer.validate_draft(text) if text else (None, [])
        return {"d": meta, "text": text, "seen_sha": text_hash(text), "doc": doc,
                "problems": problems if problems is not None else found, "notice": notice,
                "summary": draft_summary(doc) if doc else None,
                "refresh": REFRESH_S if meta.status == "drafting" else None,
                "cost": fmt_cost(meta.cost_usd) if meta.cost_usd is not None else "–",
                "provider_ok": check_provider(settings).ok}

    @app.get("/drafts/new", response_class=HTMLResponse)
    def new_draft(request: Request) -> Response:
        return render(request, "drafts_new.html.j2", problems=[], busy=draft_runner.busy(),
                      provider_ok=check_provider(settings).ok, description="")

    @app.post("/drafts", dependencies=[Depends(require_csrf)])
    async def create_draft(request: Request) -> Response:
        form = await request.form()
        limit = settings.cfg.context_max_total_chars
        reviewed = form.get("entities")
        if isinstance(reviewed, UploadFile) and reviewed.filename:
            raw = await reviewed.read(limit + 1)
            return await run_in_threadpool(begin_upload, request, raw, reviewed.filename)
        sources: dict[str, str] = {}
        description = form.get("description")
        if isinstance(description, str) and description.strip():
            sources["description.md"] = description
        for item in form.getlist("documents"):
            if isinstance(item, UploadFile) and item.filename:
                raw = await item.read(limit + 1)
                try:
                    sources[safe_name(item.filename)] = raw.decode()
                except UnicodeDecodeError:
                    return draft_form_error(request, f"{item.filename} is not UTF-8 text.")
        return await run_in_threadpool(begin_draft, request, sources,
                                       description if isinstance(description, str) else "")

    def draft_form_error(request: Request, problem: str, description: str = "",
                         status: int = 422) -> Response:
        return render(request, "drafts_new.html.j2", status, problems=[problem],
                      busy=draft_runner.busy(), provider_ok=check_provider(settings).ok,
                      description=description)

    def begin_upload(request: Request, raw: bytes, filename: str) -> Response:
        if len(raw) > settings.cfg.context_max_total_chars:
            return draft_form_error(request, "That file is larger than an entities file may be.",
                                    status=413)
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            return draft_form_error(request, "The file is not UTF-8 text.")
        meta = draft_store.create("uploaded", {filename: text}, status="ready",
                                  entities_text=set_approved(text, False))
        return RedirectResponse(f"/drafts/{meta.id}", status_code=303)

    def begin_draft(request: Request, sources: dict[str, str], description: str) -> Response:
        if not sources:
            return draft_form_error(request, "Describe the app or choose at least one document.")
        documents = [spec_writer.Document(name, text) for name, text in sources.items()]
        try:
            spec_writer.check_size(documents, settings.cfg.context_max_total_chars)
        except spec_writer.DraftTooLarge as exc:
            return draft_form_error(request, str(exc), description, status=413)
        if not check_provider(settings).ok:
            return draft_form_error(request, "No LLM provider is configured, so nothing can be "
                                    "drafted. Configure one in .env and restart the UI.",
                                    description)
        if draft_runner.busy():
            return draft_form_error(request, "Another draft is being written; wait for it.",
                                    description, status=409)
        meta = draft_store.create("generated", sources)
        draft_runner.start(meta.id)
        return RedirectResponse(f"/drafts/{meta.id}", status_code=303)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_page(request: Request, draft_id: str) -> Response:
        return render(request, "draft.html.j2", **draft_context(draft_or_404(draft_id)))

    @app.post("/drafts/{draft_id}/save", dependencies=[Depends(require_csrf)])
    async def save_draft(request: Request, draft_id: str) -> Response:
        meta = draft_or_404(draft_id)
        text = (await request.form()).get("text")
        if not isinstance(text, str) or len(text) > settings.cfg.context_max_total_chars:
            return message(request, 400, "Not saved", "The text is missing or too long.")
        draft_store.write_text(draft_id, set_approved(text.replace("\r\n", "\n"), False))
        if meta.status == "failed":
            draft_store.update(draft_id, status="ready", message="")
        return render(request, "draft.html.j2",
                      **draft_context(draft_or_404(draft_id), notice="Saved (not approved)."))

    @app.post("/drafts/{draft_id}/approve", dependencies=[Depends(require_csrf)])
    async def approve_draft(request: Request, draft_id: str) -> Response:
        meta = draft_or_404(draft_id)
        form = await request.form()
        if meta.status != "ready" or not draft_store.has_text(draft_id):
            return message(request, 409, "Not ready", "This draft cannot be approved yet.")
        text = draft_store.read_text(draft_id)
        problems: list[str] = []
        if form.get("confirm") is None:
            problems.append("Tick the box to confirm you have read every entity, rule and gap.")
        if form.get("seen_sha") != text_hash(text):
            problems.append("The file changed since this page was shown; read it again.")
        _, found = spec_writer.validate_draft(text)
        problems += found
        if problems:
            return render(request, "draft.html.j2", 422, **draft_context(meta, problems))
        response = await run_in_threadpool(begin_build, request, set_approved(text, True).encode(),
                                           f"{draft_id}.yaml", form.get("no_llm") is not None)
        location = response.headers.get("location", "")
        if response.status_code == 303 and location.startswith("/runs/"):
            draft_store.update(draft_id, approved_at=datetime.now(UTC),
                               build_run_id=location.removeprefix("/runs/"))
        return response

    @app.get("/drafts/{draft_id}/download")
    def download_draft(draft_id: str) -> Response:
        draft_or_404(draft_id)
        if not draft_store.has_text(draft_id):
            raise HTTPException(status_code=404)
        return Response(draft_store.read_text(draft_id), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{draft_id}-entities.yaml"'})

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(require_csrf)])
    async def cancel_build(request: Request, run_id: str) -> Response:
        force = (await request.form()).get("force") == "1"
        if not valid_run_id(run_id) or not build_runner.cancel(run_id, force=force):
            return message(request, 409, "Not running", "That build is not running (any more).")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}/files", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/files/{path:path}", response_class=HTMLResponse)
    def view_files(request: Request, run_id: str, path: str = "") -> Response:
        directory = run_dir(run_id)
        try:
            target = files.resolve(directory, path)
        except files.NotViewable:
            raise HTTPException(status_code=404) from None
        is_dir = target.is_dir()
        return render(
            request, "files.html.j2", run_id=run_id, shown=path or "files",
            crumbs=files.breadcrumbs(path), limit=files.MAX_VIEW_BYTES,
            entries=files.list_dir(directory, target) if is_dir else None,
            view=None if is_dir else files.read_text(target))

    @app.get("/runs/{run_id}/download/{what}")
    def download(run_id: str, what: str) -> Response:
        directory = run_dir(run_id)
        project = directory / "project"
        try:
            if what == "project":
                body = project_archive(project, run_id)
                name, kind = f"{run_id}.zip", "application/zip"
            elif what == "client":
                body = client_archive(project, f"{run_id}-client")
                name, kind = f"{run_id}-client.zip", "application/zip"
            elif what == "openapi" and not (project / "openapi.yaml").is_symlink():
                body = (project / "openapi.yaml").read_bytes()
                name, kind = f"{run_id}-openapi.yaml", "text/plain; charset=utf-8"
            else:
                raise HTTPException(status_code=404)
        except FileNotFoundError:
            raise HTTPException(status_code=404) from None
        return Response(body, media_type=kind,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        if not settings.auth_token:
            return RedirectResponse("/", status_code=303)
        return message(request, 200, "Sign in", "Enter the access token (FACTORY_UI_TOKEN).",
                       login=True)

    @app.post("/login", dependencies=[Depends(require_csrf)])
    async def login(request: Request) -> Response:
        if not settings.auth_token:
            return RedirectResponse("/", status_code=303)
        supplied = (await request.form()).get("token")
        if not isinstance(supplied, str) or not tokens_match(supplied, settings.auth_token):
            return message(request, 401, "Sign in", "That token is not right.", login=True)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, settings.auth_token, httponly=True, samesite="strict",
                            path="/")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        heading, text = _REASONS.get(exc.status_code, ("Request refused", str(exc.detail)))
        if exc.status_code == 403:  # our own fixed messages (CSRF, origin), safe to show
            heading, text = "Request refused", str(exc.detail)
        elif exc.status_code not in _REASONS:
            text = "That request could not be completed."
        return message(request, exc.status_code, heading, text)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        return message(request, 400, "Request not understood", "Check the form and try again.")

    return app
