# API Factory

A proof-of-concept **software factory**: give it an OpenAPI 3.x spec, or a short approved
`entities.yaml` describing a backend, and it produces a working, verified FastAPI backend.

```bash
uv run factory build specs/orders.yaml                  # an OpenAPI spec: single resource, SQLite
uv run factory draft examples/tasks/description.md -o entities.yaml   # prose -> a draft to review
uv run factory build entities.yaml                      # an approved entities file: full backend
```

An entities file produces several related resources, user authentication with per-user ownership,
Postgres with Alembic migrations, a Docker setup, and a client SDK, all verified. See
[Entities input](#entities-input).

The point of the PoC is **harness engineering**. Output quality comes from the structure around the
model: curated context, three tools, guardrails, and verification the model cannot fake. The LLM
only writes business logic and behaviour tests; everything that can be generated mechanically is.

## Pipeline

| # | Station | How | Output |
|---|---|---|---|
| 1 | `lint` | openapi-spec-validator + factory rules | errors stop the run |
| 2 | `scaffold` | datamodel-code-generator + Jinja2 | models, route + service stubs, db/main, fixtures, project `AGENTS.md` |
| – | `setup` | `uv sync` in the project | the generated project's own virtualenv |
| 3 | `test_writer` | LLM agent | behaviour tests, accepted only through the anti-cheating gates |
| 4 | `coder` | LLM agent loop | business logic, iterated until ruff/mypy/bandit/pytest/Schemathesis pass |
| 5 | `contract` | Schemathesis vs a live uvicorn | final contract result on a fresh database |
| 6 | `sdk` | openapi-python-client | `client/`, checked importable |
| 7 | report | Jinja2 | `run.json` + `report.md`, rewritten after every station and iteration |

Each run lives in `runs/<run_id>/`: `project/` (the generated backend), `report.md`, `run.json`,
`iterations/*.diff`, and full logs of every check under `checks/` and `contract/`.

## Entities input

An entities file is the short, typed description of a backend that a human reads and approves.
Everything mechanical is generated from it without an LLM; only the **business rules** and the
bodies of **actions** are written by the coder agent, behind guardrails. The model never decides
what the database looks like.

```yaml
approved: true            # a human sets this after reading the file; build refuses it otherwise
app: tasks
entities:
  Project:
    owner: user           # rows belong to their creator; other users get 404 (required, explicit)
    fields:
      name: {type: str, min: 1, max: 200}
      archived: {type: bool, default: false}
    rules:                # needs code, so the coder writes it: ids, statuses, one sentence each
      - {id: project-limit, text: "At most 20 non-archived projects; the 21st returns 409.",
         on: [Project.create], status: 409}
    actions:
      archive: {text: "Marks the project archived.", returns: Project}
  Task:
    parent: Project       # /projects/{project_id}/tasks, deleted with its project
    fields:
      title: {type: str, min: 1, max: 200}
      status: {type: enum, values: [todo, doing, done], default: todo,
               transitions: {todo: [doing], doing: [done]}}   # generated, not written by a model
    guards:               # one simple comparison, generated too
      - {id: archived-project-rejects-tasks, on: Task.create,
         when: "parent.archived == true", status: 409}
```

The full sample is [examples/tasks/entities.yaml](examples/tasks/entities.yaml); its prose version,
[examples/tasks/description.md](examples/tasks/description.md), is what `factory draft` reads.
The format and every rule it enforces are in `factory/entities.py`.

| # | Station | How | Output |
|---|---|---|---|
| – | `draft` (`factory draft`) | LLM, no tools, schema-validated, retried with the exact errors | `entities.yaml` with `approved: false` and a review banner |
| 1 | `lint` | validate, require `approved: true`, expand to OpenAPI, same OpenAPI lint | errors stop the run |
| 2 | `scaffold` | Jinja2 + datamodel-codegen | ORM, Alembic migration, settings, JWT auth, CRUD with ownership, routes with guards and transitions, rule-hook stubs, Dockerfile, compose file, generated tests |
| – | `setup` | `uv sync` | the project's own virtualenv |
| 3 | `postgres` | Docker container, or local binaries | one server per run, a template database migrated with `alembic upgrade head` |
| 4 | `test_writer` | LLM agent (skipped when there are no rules or actions) | behaviour tests for the rules and actions only, behind the v2 gates |
| 5 | `coder` | LLM agent loop | the hooks in `app/rules/`, and nothing else |
| 6 | `contract` | Schemathesis with a signed-up user's token | contract result on a fresh cloned database |
| 7 | `sdk` | openapi-python-client | `client/` |
| 8 | `deploy_check` | `docker compose up --build --wait`, `/health`, one authenticated round trip | proof that it deploys |

**The schema is fixed by the approved file.** The ORM and the migration are generated read-only,
every check builds its database with the real `alembic upgrade head`, and DDL strings are forbidden
for the coder. If a rule seems to need a new column, that is a change to `entities.yaml` and goes
back for approval.

**Incremental builds** (`--from`, `--incremental`) are not supported for entities files yet: a
changed file needs a fresh build (see Open Question 2 in
[the design](docs/designs/description-to-backend.md) and [TODOS.md](TODOS.md)).

**Budgets** scale with the work the model does: iterations, changed lines and cost are each
`base + per_item × (rules + actions)`, capped (values in `factory/config.py`, recorded in
`run.json`).

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Entities builds also need
**Docker** (Postgres container and the deploy check), or the PostgreSQL 16 binaries with
`FACTORY_POSTGRES_BACKEND=local` (which skips the Docker deploy check).

```bash
uv sync
cp .env.example .env     # set LLM_PROVIDER, LLM_MODEL and the matching API key
uv run factory --help
```

`.env` (never committed):

| Variable | Meaning |
|---|---|
| `LLM_PROVIDER` | `anthropic`, `openai`, `deepseek`, or `scripted` (no key; replays a script) |
| `LLM_MODEL` | model id; defaults: `claude-opus-5-5` (anthropic), `deepseek-chat` (deepseek); required for openai |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` | key for the selected provider |
| `LLM_EFFORT` | Anthropic effort (`low`…`max`, default `high`; empty to omit for older models) |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | USD per million tokens, for models not in `factory/config.py` |
| `LLM_SCRIPT` | script file for `LLM_PROVIDER=scripted` |
| `FACTORY_<FIELD>` | override any scalar in `factory/config.py`, e.g. `FACTORY_MAX_ITERS=4`, `FACTORY_MAX_COST_USD_PER_RUN=1`; for entities builds `FACTORY_POSTGRES_BACKEND=docker\|local`, `FACTORY_DEPLOY_CHECK=0`, `FACTORY_BUDGET_ITERS_CAP=4` (entities budgets are computed, so override their caps) |

## Usage

```bash
uv run factory llm-ping                                   # one tool-calling request; nothing executed
uv run factory build specs/orders.yaml                    # full build
uv run factory build specs/orders.yaml --no-llm           # deterministic stations only
uv run factory build specs/orders_v2.yaml --from <run_id> # incremental build on a previous run
uv run factory build specs/orders.yaml --incremental      # ...on the latest passed run of this spec
uv run factory draft examples/tasks/description.md -o entities.yaml   # draft; --force to overwrite
# review entities.yaml, then change `approved: false` to `approved: true`
uv run factory build entities.yaml                        # entities file -> full backend
uv run factory build entities.yaml --no-llm               # generate and check, no agents
uv run factory runs                                       # list runs
uv run factory report latest                              # print report.md (--json for run.json)
```

`build` exits 0 only when the run status is `passed` (130 when cancelled with Ctrl-C).

### Web UI

```bash
uv run factory ui                    # http://127.0.0.1:8765, this machine only
```

The UI does everything above from a browser: describe an app and have the AI draft an entities
file (or upload one), read a plain-language summary of every entity, rule and gap, edit and save
it, then tick "I have read…" and approve, which starts the build. A run page shows each station,
iteration, check, cheat probe, budget and the console while it runs (the page reloads by itself),
with Cancel, a file viewer, and downloads of the project, the client SDK and the OpenAPI file.
One build runs at a time, as a separate process, so a crash or Cancel (Ctrl-C) cleans up like the
command line does.

Pages are server-rendered with no scripts (the repo is Python only). It listens on `127.0.0.1`
and refuses other Host and Origin values; every form carries a per-process CSRF token. To reach it
from another machine set `FACTORY_UI_TOKEN` and pass `--allow-remote` (plus
`FACTORY_UI_ALLOWED_HOSTS=name1,name2` for the host names you will use); there is no TLS, so put
it behind a tunnel or reverse proxy. Saving a draft never approves it; only the approve button
does, and only for the exact text shown. Drafts are stored under `drafts/`.

### Without an API key

The `scripted` provider replays agent tool calls from a YAML script through the real harness. Every
station, guardrail, check and report is real; only the model is replaced. Scripts can assert that
the harness actually fed failures back (`expect:`).

```bash
export LLM_PROVIDER=scripted
LLM_SCRIPT=tests/fixtures/scripted/orders_v1/script.yaml uv run factory build specs/orders.yaml --run-id v1
LLM_SCRIPT=tests/fixtures/scripted/orders_v2/script.yaml uv run factory build specs/orders_v2.yaml --from v1
FACTORY_MAX_ITERS=2 LLM_SCRIPT=tests/fixtures/scripted/orders_cheat/script.yaml uv run factory build specs/orders.yaml
```

The entities scenarios work the same way (they need Postgres: Docker, or
`FACTORY_POSTGRES_BACKEND=local`):

```bash
LLM_SCRIPT=tests/fixtures/scripted/tasks_v1/script.yaml uv run factory build examples/tasks/entities.yaml
FACTORY_BUDGET_ITERS_CAP=2 LLM_SCRIPT=tests/fixtures/scripted/tasks_cheat/script.yaml uv run factory build examples/tasks/entities.yaml
LLM_SCRIPT=tests/fixtures/scripted/tasks_draft/script.yaml uv run factory draft examples/tasks/description.md -o /tmp/entities.yaml
```

`tasks_v1`: a one-sided test suite (only checks rejections) is rejected by the "always reject"
probe, then a complete one is accepted; the coder tries to edit a generated test and a generated
CRUD module (both blocked), ships a project limit that is off by one, and fixes it from the failing
test. `tasks_cheat`: a coder whose rules accept everything fails the build. `tasks_draft`: an
invalid first draft is corrected after the validator's errors are fed back.

The v1 script includes a weak first test suite (rejected by the gates), a test writer trying to
read the implementation and a coder trying to edit the tests (both blocked and recorded), and a buggy
first implementation fixed from the fed-back pytest failure. The cheat script ships a hardcoded
implementation. It passes Schemathesis but fails the behaviour suite, so the run fails.

## How the harness works

**Context.** Every coder iteration rebuilds context from disk: the spec, the project `AGENTS.md`, the
file tree, the current code and tests, and a bounded failure summary from the last check run
(behaviour failures first). No conversation history accumulates across iterations. Prompts are
Jinja2 templates in `factory/templates/prompts/`. Their hashes are recorded per run.

**Tools.** `read_file`, `list_files`, `write_file`, via each provider's native tool calling,
capped at 20 calls per iteration. `run_checks` is not a tool. The harness runs checks after the
agent's turn.

**Guardrails** (`factory/harness/guardrails.py`, limits in `factory/config.py`):

| Rule | Behaviour |
|---|---|
| Coder writes only `app/routes/**`, `app/services/**`, `app/db.py`, `app/main.py` (`.py`) | write blocked, violation recorded |
| `openapi.yaml`, `app/models.py`, `tests/**` (for the coder), `tests/conftest.py`, `AGENTS.md`, `pyproject.toml` are read-only | write blocked, violation recorded |
| Test writer writes only `tests/test_*.py` and cannot read `app/routes`, `app/services`, `app/db.py` | blocked, violation recorded |
| Path traversal, absolute paths, symlinks out of the project | blocked, violation recorded |
| Forbidden code: `eval(`, `exec(`, `subprocess`, `os.system`, `importlib`, `__import__`, `pytest.skip`/`xfail` (incl. marks), more than two `# type: ignore` per file | write blocked, violation recorded |
| More than 400 changed lines in one iteration | iteration reverted and rejected |
| More than 20 tool calls in one iteration | turn ended, violation recorded |
| Command timeout (120 s) | process group killed, check fails |
| Token / cost budget per run | run stops with `budget_exhausted` |
| Generated code gets a scrubbed environment (no API keys) | enforced |
| No network beyond localhost | **documented only**; enforcement planned via a Docker sandbox |

**Guardrails under the entities profile** (same engine, different lists in `factory/config.py`):

| Rule | Behaviour |
|---|---|
| The coder writes only `app/rules/**` | everything else generated is read-only (`ENTITIES_READ_ONLY`: routes, CRUD, ORM, auth, settings, migrations, tests/generated, Dockerfile, compose, `entities.yaml`) |
| The test writer writes only `tests/test_*.py` and cannot read `app/rules/**` | blocked, violation recorded |
| `create_table`, `ALTER TABLE`, `metadata.create_all`, `alembic` in agent code | write blocked: a tripwire; the enforcement is the read-only ORM and migrations |
| A hook can only reject with a rule id declared in `entities.yaml` | the status code is fixed by the file (`app/errors.py`) |

**Gates for the test writer's suite under the entities profile** (`factory/harness/test_quality.py`).
Generated tests in `tests/generated/` already cover authentication, ownership, CRUD, paging, guards
and state transitions, so the writer is held to the rules and actions only, and the gates run its
files alone (a generated test can never make a weak suite look good):
1. it collects;
2. every rule id appears in a `@pytest.mark.rule("<id>")` marker and every action is called;
3. it **fails** against the unimplemented hooks (red check);
4. it **fails** against a rule set that never rejects, **and** against one that rejects everything a
   rule can reject. A suite that only tests rejections, or only acceptance, is sent back.

**Verification** (`factory/harness/checks.py`). Each iteration runs ruff, mypy `--strict`, bandit,
pytest and Schemathesis (`--checks all`, positive, negative and stateful phases) against a live
server with a fresh database. Schemathesis runs only once pytest passes. The loop exits only on real
exit codes and parsed reports.

**Anti-cheating** (`factory/harness/test_quality.py`). A test suite is accepted only if it:
1. collects;
2. passes a static gate: every operation exercised, a create → read-back test per resource, and at
   least one test with varied inputs;
3. **fails** against the unimplemented stubs (red check);
4. **fails** against a generated, deliberately hardcoded implementation that returns the spec's
   examples (cheat probe).

The test writer never sees the implementation; the coder can read the tests but not change them.

**Deterministic contract plumbing.** Where Pydantic or FastAPI defaults disagree with the spec, the
scaffold fixes it in generated code instead of asking the model:
- strict scalars (`false` is not a number);
- an int type that accepts `3.0` (JSON Schema) but rejects `"3"` and `true`;
- optional request properties reject explicit `null` unless the spec marks them nullable;
- undecodable bodies return the documented 422 instead of 400;
- correct `Allow` headers on 405 responses.

All of these were found by Schemathesis during development.

**Incremental builds.** With `--from`, the previous project is copied. Generated files that no agent
touched (hash matches `.factory/manifest.json`) are regenerated. Agent-edited files are kept, and
stubs for new operations are appended. Both agents receive the spec diff. `report.md` lists the
spec changes and a per-file diff against the previous run.

## Development

```bash
uv run ruff check . && uv run mypy && uv run pytest     # unit tests (fast)
FACTORY_E2E=1 uv run pytest tests/test_e2e.py           # end-to-end builds (~15 min, needs Docker)
FACTORY_E2E=1 FACTORY_POSTGRES_BACKEND=local uv run pytest tests/test_e2e.py   # no Docker
FACTORY_EVAL=1 uv run pytest tests/test_eval_spec_writer.py -s   # live spec-writer eval (API key)
```

What the end-to-end tests prove, against a real Postgres: the OpenAPI builds still pass with
unchanged assertions; an entities build passes with scripted agents (rejected suite, blocked
writes, a bug fixed from the failing test, Schemathesis with auth, the SDK, and `docker compose`
when Docker is used); and a coder that skips the rules fails the build. Tests in
`tests/test_scaffold_entities.py` also run the *generated* project's own tests against Postgres,
and deliberately break ownership, paging, a guard, a transition and authentication to check that
the generated tests catch each.

See [AGENTS.md](AGENTS.md) for the rules for anyone (human or AI) changing the factory.

## Layout

```
factory/
  cli.py            build, draft, report, runs, llm-ping
  config.py         budgets, limits, allowlists, rule profiles, model prices
  pipeline.py       orchestrates the stations (an OpenAPI flow and an entities flow)
  entities.py       the entities format, its validation, and expand() to OpenAPI
  spec.py           typed view of the OpenAPI spec shared by all stations
  run_context.py    per-run state
  stations/         lint, scaffold, scaffold_entities, test_writer, coder, contract (+ deploy
                    check), sdk, incremental, spec_writer
  harness/          context, tools, guardrails, loop, sandbox, checks, check_env, postgres,
                    test_quality
  llm/              base interface, anthropic, openai_compat (OpenAI + DeepSeek), scripted, pricing
  report/           run record model + run.json / report.md writer
  templates/        project/ (OpenAPI-profile code), project_v2/ (entities-profile code),
                    prompts/ (agent prompts), report/
specs/              orders.yaml (v1.0), orders_v2.yaml (v1.1: note + cancel)
examples/tasks/     description.md (prose) and entities.yaml (the approved sample)
docs/               designs/ (the approved design), plans/ (the implementation plan)
runs/               one folder per run (gitignored)
tests/              factory tests; fixtures/scripted/ holds the scripted scenarios
```

## Roadmap

- [x] Phase 0: repo setup, sample spec, CLI skeleton
- [x] Phase 1: deterministic pipeline (lint, scaffold, SDK, failing Schemathesis run)
- [x] Phase 2: LLM provider interface, tools, guardrails
- [x] Phase 3: coder loop
- [x] Phase 4: test-writer agent + anti-cheating gates
- [x] Phase 5: reporting
- [x] Phase 6: incremental builds on spec changes
- [x] Phase 7: entities format and `expand()` to OpenAPI
- [x] Phase 8: project template v2 (Postgres, Alembic, JWT auth, CRUD with ownership, hooks)
- [x] Phase 9: gates v2 (rule markers, writer-only red check, two cheat probes, Schemathesis auth,
  budgets that scale with the work)
- [x] Phase 10: `factory draft` (spec writer) and the opt-in live eval
- [x] Phase 11: deploy check and report updates
- [ ] Later: Docker sandbox with no network, GitHub Actions trigger, review agent, benchmark mode,
  incremental builds for entities files (migrations across versions, see [TODOS.md](TODOS.md))

### What has and has not been verified

Verified with a real Postgres 16 and scripted agents: the generator and its generated tests (also
by deliberately breaking ownership, paging, guards, transitions and authentication), the gates on
one-sided and complete test suites, Schemathesis with auth (1054 cases, no failures), the full
scripted builds and the unchanged OpenAPI builds.

Not yet verified, and listed in [TODOS.md](TODOS.md): the **Docker** Postgres backend and the
`docker compose` deploy check (no Docker daemon was available while building them; the generated
compose file was validated with `docker compose config`), a **real model** writing a draft, test
suites or rules (only scripted agents so far; the opt-in eval is for this), and **the Assignment**
from the design: the format has not yet been shown to an engineer with a real project description.
