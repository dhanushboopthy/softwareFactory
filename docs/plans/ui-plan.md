# A simple UI for the factory

Status: plan only, nothing built. Written 2026-10-26.

## What the UI is for

The factory already works from the command line. The UI makes the **human steps** easy and the
**run progress** visible, without changing how builds work:

1. Write or upload a project description, get a **draft** `entities.yaml`.
2. **Review, edit and approve** it. This is the one step the checks cannot replace, so the UI makes
   it deliberate and hard to skip.
3. **Start a build** and watch it: stations, agent iterations, gate results, violations, cost.
4. Browse past runs; download the generated project, its OpenAPI spec and the client SDK.

Not in scope: multi-user accounts, hosting it on a network, editing generated code, chat, or
incremental builds (until the migrations work exists).

## The constraint that decides the stack

`AGENTS.md` rule 6: **Python only. No JavaScript/TypeScript or Node-based tooling anywhere.**

| Option | Verdict |
|---|---|
| **A. FastAPI + Jinja2, server-rendered HTML, plain CSS, no JavaScript** | **Recommended.** Same stack as the rest of the repo (FastAPI, Jinja2, uvicorn, TestClient). Every page is testable without a browser. Progress uses a page refresh. |
| B. NiceGUI | Python-only for us, but it ships and drives a JavaScript front end; heavy dependency and harder to test. |
| C. Streamlit | Its rerun model fits badly with long background builds and a one-time approval step. |
| D. React/htmx front end | Rejected by rule 6 (amend the rule first if you want live, no-refresh updates or a YAML editor with highlighting). |

Trade-off accepted with A: the run page reloads every 3 seconds while a build is running
(`<meta http-equiv="refresh">`), and the YAML editor is a plain textarea.

## Key design decisions

1. **No new state for progress.** The pipeline already writes `runs/<id>/run.json` after every
   station and every agent iteration. The run page just reads it (`report.writer.load_record`).
2. **Builds run as a subprocess** (`factory build ...`), not a thread. A crash cannot take the
   server down, and **Cancel** is a clean SIGINT to the process group, so the existing `finally`
   blocks still stop Postgres and run `docker compose down`. (Needs one small backend change: a
   `cancelled` run status and handling of `KeyboardInterrupt` in `pipeline.build`.)
3. **One build at a time.** A build uses Postgres, `uv`, possibly Docker and LLM money; a second
   request is refused with a message ("run X is in progress"), not queued silently.
4. **Drafts run in-process** (a few LLM calls, no heavy side effects) and are stored in a gitignored
   `drafts/<id>/` folder: `entities.yaml` plus `draft.json` (attempts, cost, last errors).
5. **Approval is explicit and recorded.** The review page shows the entities in a readable summary
   plus the raw YAML; approving needs a checkbox ("I have read every entity, owner, rule and gap")
   and a button. The server rewrites `approved: false` to `approved: true`; the build records the
   file's sha256 and `approved` flag (already in `run.json`).
6. **It is a local tool and must be safe on localhost.**
   - Binds to `127.0.0.1` only. Binding elsewhere needs `--allow-remote` **and** a token
     (`FACTORY_UI_TOKEN`); the UI refuses to start otherwise.
   - Any web page you have open can send requests to `localhost`, and a POST here starts a paid
     build that runs generated code. So every POST needs a per-process random CSRF token, and the
     `Host` and `Origin` headers must be the UI's own (this also blocks DNS rebinding).
   - Autoescaping is on in the UI templates (the factory's other Jinja environments render text and
     have it off). Run ids are validated (`[A-Za-z0-9._-]+`); the file browser and downloads resolve
     paths inside the run directory and refuse symlinks and `..`.
   - Uploads are capped at `context_max_total_chars`. API keys are never rendered; the settings
     panel only says whether one is set.
7. **Same configuration as the CLI**: `.env`, `FACTORY_*` variables, the same provider selection.

## Pages

```
GET  /                    dashboard: environment panel, "New project", table of runs
GET  /new                 paste or upload description(s)           POST /new   -> draft job
GET  /drafts/<id>         draft status; review/edit/validate/approve
POST /drafts/<id>/save    save edits         POST /drafts/<id>/validate    POST /drafts/<id>/approve
POST /builds              start a build from an approved file (drafts/ or an uploaded entities.yaml)
GET  /runs/<id>           live run page (refreshes while running)  POST /runs/<id>/cancel
GET  /runs/<id>/files/<path>      read-only file viewer       GET /runs/<id>/download/<what>
GET  /runs/<id>/report    report.md rendered as text
```

Dashboard:

```
 API Factory                                        provider: anthropic / claude-opus-5-5  ✓
 Postgres: docker ✓ | Docker daemon ✓ | uv ✓                                   [ New project ]
 ┌ Runs ─────────────────────────────────────────────────────────────────────────────────────┐
 │ ▶ 20261026-1904-tasks   entities   running  test_writer #1     $0.42   [open] [cancel]     │
 │   20261026-1722-tasks   entities   passed   9 stations         $1.10   [open]              │
 │   v1                    openapi    passed   11 iterations      $0.31   [open]              │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
```

Draft review (the important page):

```
 Draft of examples/tasks/description.md           attempts 2 · $0.03 · approved: NO
 ┌ Summary ────────────────────────┐ ┌ entities.yaml (editable) ───────────────────────────┐
 │ Project  owner: user            │ │ approved: false                                     │
 │   name, archived                │ │ app: tasks                                          │
 │   rules: project-limit (LLM)    │ │ entities: ...                                       │
 │   action: archive (LLM)         │ │                                                     │
 │ Task  parent: Project           │ └─────────────────────────────────────────────────────┘
 │   status: todo→doing→done (gen) │  [ Save ] [ Validate ]       Problems: none ✓
 │   guard: archived-project...    │
 │ LLM work: 3 items → budget:     │  Gaps the model reported (read these):
 │   10 iterations, $3.25 max      │   • "Paging cannot be turned off."
 └─────────────────────────────────┘
 [x] I have read every entity, owner, rule and gap above.        [ Approve and build ]
```

Run page: status header and budget bars; stations with ✅/❌/⏭ and details; per-iteration table
(agent, checks as pass/fail chips, violations); test-writer gate results including the two cheat
probes ("caught ✅ / NOT caught ❌"); contract and deploy results; violations; a live log tail;
links to diffs, the report and downloads.

## Code layout

```
factory/ui/
  app.py        create_app(cfg): routes, security middleware (host/origin/CSRF/token)
  jobs.py       BuildRunner (subprocess, one at a time, cancel), DraftStore
  views.py      pure functions turning RunRecord / EntitiesDoc into template-ready dicts
  environment.py  checks shown on the dashboard (provider set, docker info, uv found)
  templates/*.html.j2   base, dashboard, new, draft, run, file view   (autoescape on)
  static/ui.css         the only static asset
factory/cli.py          `factory ui [--port 8765] [--host 127.0.0.1] [--allow-remote]`
```

New dependencies (added with `uv add`): `fastapi`, `uvicorn`, `python-multipart` (uploads).

## Build order

| Step | Content | Size |
|---|---|---|
| UI-0 | Backend prerequisites: `cancelled` status + `KeyboardInterrupt` handling in `pipeline.build`; `get_provider(env)` so a job can choose its own environment; `export_project()` (zip without `.venv`, caches, `.env`); a cheap run summary for the dashboard. Unit tests; the OpenAPI e2e tests must still pass unchanged. | S |
| UI-1 | Skeleton: `factory ui`, `create_app`, base template and CSS, the security middleware, the environment panel. Tests: rejected Host/Origin/CSRF, remote bind refused without a token, escaping of hostile run names. | M |
| UI-2 | Dashboard, run page (all sections above), file viewer and downloads, meta refresh while running. Tests from fixture `run.json` files, including a hostile file name, path traversal and symlink attempts. | M |
| UI-3 | Build runner: start from an approved file, one at a time, live log, cancel. Tests with a fake `factory` command; one real scripted build driven through the UI. | M |
| UI-4 | Drafts: new/upload, draft job, review page (summary, edit, validate, gaps), approval gate, "upload an existing entities.yaml". Tests with the scripted spec writer. | M |
| UI-5 | Error and empty states (provider not configured, Docker down with the fix, oversize input with sizes), README section, a manual browser pass over the real pages. | S |

Rough effort: about one focused day with the same pace as the v2 core. UI-0 and UI-1 first; after
UI-2 you can already watch CLI-started builds, so each step is useful on its own.

## Risks and how the plan handles them

| Risk | Handling |
|---|---|
| A web page triggers a paid build on your machine | CSRF token, Host/Origin checks, localhost bind, no cookies or ambient credentials |
| Cancelling leaves Postgres/containers behind | SIGINT lets the existing `finally` blocks run; a test asserts the compose/Postgres cleanup path |
| Plain textarea feels basic for 80 lines of YAML | Validation on demand and a readable summary next to it; revisit only if rule 6 is changed |
| Auto-refresh loses scroll position | Only the run page refreshes, and it keeps long sections collapsed; the live log is the last element |
| Browser-level behaviour is untested | Server-rendered pages are covered by TestClient; UI-5 includes one manual browser pass, and the real draft/build flows still depend on the unverified items in TODOS.md (Docker, a real model) |
