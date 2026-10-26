# v2 implementation plan (tasks T2–T13)

Companion to `docs/designs/description-to-backend.md` (the approved design). The design says
**what** and **why**; this file says **exactly what to build, in which files, in which order, and
how to prove it works**. It is written so that each task can be handed to a fresh Claude session
(e.g. Sonnet) on its own.

Status on 2026-10-26: **T1-T13 are all implemented** (T1 `cde9f12`, T2-T7 `c14fa78`, the rest in
the commits after it). ruff and mypy are clean; the full suite with the real-database tests
(`FACTORY_E2E=1 FACTORY_POSTGRES_BACKEND=local pytest`) is green, including the six end-to-end
builds (three v1 tests with unchanged assertions, three entities scenarios).

What is **not** verified, and why (see TODOS.md): the Docker Postgres backend and the
`docker compose` deploy check (no Docker daemon was available; only `docker compose config` was
run), a real LLM (everything uses scripted agents; the live eval is opt in), and the Assignment
(R15; the format has not been shown to an engineer with a real description).

### Decisions made while building T8-T10

- **T8:** `pipeline.build` has two flows, `_build_openapi` (unchanged) and `_build_entities`
  (lint incl. approval, scaffold, setup, postgres, test_writer or skip, baseline, coder, contract,
  sdk). The Postgres server lives in an `ExitStack` for the whole run and is always stopped.
  Refusals: invalid file and unapproved file are failed `lint` stations (messages in
  `pipeline.NOT_APPROVED`); `--from`/`--incremental` raise `IncrementalNotSupported` before a run
  directory exists, and the CLI turns that into exit 2. Budgets: `FactoryConfig.scaled_for(items)`
  with the `budget_*` fields (env-overridable caps, e.g. `FACTORY_BUDGET_ITERS_CAP`), recorded in
  `RunRecord.budgets`; `RunRecord` also has `profile` and `approved`.
- **T9:** scenarios are `tests/fixtures/scripted/tasks_v1` (one-sided suite rejected by the
  "always reject" probe, then a good one; a coder that edits generated files and ships an
  off-by-one, then fixes it) and `tasks_cheat` (a coder that accepts everything; the run fails).
  The e2e tests read `FACTORY_POSTGRES_BACKEND` (default `docker`).
- **T10:** `factory draft DOCS... -o FILE [--force]` -> `stations/spec_writer.py`. One tool-less
  call per attempt; the answer is validated by the build's own schema plus the OpenAPI lint; the
  exact errors and the previous answer are fed back (`spec_writer_max_attempts`); only a valid
  draft is written, with `approved: false` as its first key and a review banner; oversize input is
  refused before any model call. The providers now omit the `tools` key when there are none (the
  OpenAI API rejects an empty list). The system prompt's example is checked against
  `examples/tasks/entities.yaml` by a test, so they cannot drift.
- **Gotcha for anyone loading an entities file in Python:** use `load_entities_raw` /
  `load_entities_text`, never `yaml.safe_load` (it reads an unquoted `on:` key as `True`).

### Decisions made while building T6-T7

- **T6:** `CheckEnv` (harness/check_env.py) owns the template database, the per-run secret and
  `fresh_db/env_for/fresh_env`. `checks.run_all(..., check_env)`, `run_schemathesis(..., check_env)`
  and `contract.run_contract(..., check_env)` switch the entities profile on; `check_env=None` is
  the unchanged v1 behaviour. Schemathesis with auth on the tasks sample: 1054 cases, 0 failures,
  no per-project Schemathesis config needed (documented 409s do not trip `--checks all`).
- **T7:** `RunContext` gained `entities` and `check_env` (T8 must set them); `RunRecord` gained
  `cheat_probes`. The static gate is `assess_tests_v2`; probes are `cheat_probes_v2` (results are
  named `cheat_never_reject` / `cheat_always_reject`). `test_writer.skip_reason(ctx)` is what T8
  calls to skip the station when `llm_items() == 0`. Entities-profile read-only list is
  `config.ENTITIES_READ_ONLY`, so generated paths report `read_only` (not `path_not_allowed`).
- **Gate nuance (by design):** the "never reject" fake leaves an action's row unchanged, so a
  writer test that asserts an action's *effect* is caught by it; only a one-sided suite that checks
  status codes alone survives. The v2 prompts tell the writer to assert effects.
- **Prompts:** `test_writer_v2.*.j2` and `coder_v2.*.j2` (context builders pick them when
  `cfg.profile == "entities"`).

### Decisions made while building T2-T5 (these override the sections below where they differ)

- **Single cheat template.** `cheat_rules.py.j2` renders both variants (`mode="never"|"always"`)
  through `render_cheat_rules(doc, entity, mode)` in `scaffold_entities.py`; there are not two files.
  T7 should call that function.
- **`static_files_v2(doc, cfg)` / `scaffold_entities(doc, entities_path, project, timeout, cfg)`**
  are the entry points T8 calls. `expand(doc, cfg)` takes the list limits from
  `FactoryConfig.list_limit_default/max` (config.py is their only home).
- **Generated ruff config has no `I` (import sorting)**, so the coder is never failed on cosmetics.
- **Guard semantics:** `when: "<field> == x"` checks the request body on `create` and the stored row
  on update/delete/actions; `parent.<field>` checks the loaded parent. Guards run before hooks.
- **Sample rows avoid every guard literal** (`avoid_literals`), so a guard cannot block the generic
  generated tests. A spec whose guards make a state unreachable (e.g. create guard and delete guard
  on the same literal) is self-contradictory and its generated tests will fail; fix the spec.
- **`offset` is bounded** (`maximum: 9223372036854775807`) in the spec and the route, because
  Postgres errors on a larger OFFSET (that would be a 500 under Schemathesis).
- **Postgres backends:** `postgres_backend` is `docker` (default) or `local` (PostgreSQL binaries in
  `postgres_bin_dir`, no Docker needed). `sandbox.background()` runs the local server.
- **Verification already in place for T4:** `TestGeneratedProject` (tasks sample) and
  `TestEveryTemplateBranch` (the "ledger" document covering every template branch) run the
  generated tests against a real database, run ruff/mypy --strict/bandit on the generated code, and
  five mutation checks prove the generated tests fail when ownership, paging, a guard, a
  transition or authentication is broken.
- **Reference rules** for the sample live in `tests/fixtures/scripted/tasks_v1/files/rules_project.py`
  (T9 reuses it as the scripted coder's final answer).

---

## 0. How to use this plan

### One task per session

Give the model one task at a time, in the order of section 2. Prompt template:

> Read `AGENTS.md`, `docs/plans/v2-implementation-plan.md` (sections 0, 1 and task **T<n>**), and
> the design decisions it cites in `docs/designs/description-to-backend.md`. Implement **T<n>
> only**. Follow the "Files", "Build" and "Done when" lists exactly. Do not start the next task.
> When finished, run the verification commands in section 0 and report their real output.

After each task: review the diff in the IDE, then commit yourself (the plan never asks the model
to commit).

### Rules every session must follow (from AGENTS.md and the owner's preferences)

1. Never edit anything under `runs/`. Never import generated-project code into `factory/`.
2. Deterministic first: anything mechanical is a Jinja2 template or plain Python, never an LLM.
3. Loop exits are based on real command output only.
4. Every prompt sent to an LLM is a file in `factory/templates/prompts/`. No prompt strings in Python.
5. No agent frameworks. Python only (generated code included). No secrets in code.
6. Limits, allowlists and budgets live only in `factory/config.py`.
7. Harness code gets unit tests in `tests/`. `mypy --strict` passes on `factory/` and `tests/`.
8. Guardrail violations are recorded as data, not just raised.
9. **v1 must not change behaviour.** `specs/orders.yaml` and `orders_v2.yaml` builds, and the three
   existing e2e tests, keep passing with unchanged assertions (R7). Put v2 templates in a new
   folder (`factory/templates/project_v2/`) instead of editing v1 templates.
10. Make file changes with the Edit/Write tools (the owner reviews diffs in the IDE), not `sed -i`
    or heredocs.
11. Never add Claude as co-author; do not change git author; do not commit unless asked.
12. Add dependencies with `uv add` / `uv add --dev`, never by editing `uv.lock`.

### Verification commands (run after every task)

```bash
uv run ruff check .
uv run mypy
uv run pytest
FACTORY_E2E=1 uv run pytest tests/test_e2e.py   # when stations, templates or the harness changed
```

#### Running inside the Claude Code sandbox on this machine

The sandbox hides the home directory, blocks the Docker socket and blocks reading `*.pem` files.
If `uv` is "command not found", use these instead (all paths inside the project):

```bash
export UV="$PWD/.tools/bin/uv"                      # uv 0.13.1, checksum-verified, gitignored
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt   # certifi's cacert.pem is unreadable
export UV_CACHE_DIR="$TMPDIR/uv-cache"
.venv/bin/ruff check . ; .venv/bin/mypy ; .venv/bin/python -m pytest
FACTORY_E2E=1 .venv/bin/python -m pytest tests/test_e2e.py
```

Network: `uv sync` in generated projects needs `pypi.org` and `files.pythonhosted.org`.

**Sandbox caveat (resolved):** a `uv sync` that reuses an existing cache fails with
`Failed to copy ... certifi/cacert.pem` (the sandbox denies reading `*.pem`). Fix: delete the cache
before each e2e session (`rm -rf "$TMPDIR/uv-cache"`) so uv downloads fresh. A fresh cache works
in both copy and symlink link modes, so no factory code change is needed. Verified 2026-10-26:
`FACTORY_E2E=1 .venv/bin/python -m pytest tests/test_e2e.py` gives **3 passed in ~10 min** (this is
the v1 baseline that R7 requires to stay green). Unit baseline: 145 passed, 3 skipped. Outside the
sandbox (a normal terminal) none of this applies.

If a command fails with `bwrap: Can't get type of source .../.mcp.json`, the sandbox itself failed
to start (not your code); just re-run the command.

Docker is unavailable inside the sandbox; a local Postgres 16 is installed at
`/usr/lib/postgresql/16/bin` and works (TCP on 127.0.0.1, password auth). See T5 for the "local"
backend that uses it.

---

## 1. Fixed design decisions (do not re-decide these)

These come from the approved decision ledger (R1–R19) plus concrete choices made while planning.
Where this section is more specific than the design doc, follow this section.

### 1.1 The v2 generated project layout

```
openapi.yaml                     generated by expand(); read-only
entities.yaml                    copy of the approved input; read-only
AGENTS.md                        rendered from project_v2/AGENTS.md.j2; read-only
pyproject.toml                   read-only (adds alembic, psycopg[binary], pyjwt)
alembic.ini                      read-only
migrations/env.py                read-only; reads DATABASE_URL from app.settings
migrations/versions/0001_initial.py   read-only; generated from entities (indexes on owner_id + parent fks)
.env.example                     DATABASE_URL=..., JWT_SECRET= (empty), PASSWORD_HASH_ITERATIONS=
Dockerfile, compose.yaml         read-only (used in T12)
app/__init__.py
app/models.py                    datamodel-code-generator + harden_models (reuse scaffold.py)
app/settings.py                  DATABASE_URL, JWT_SECRET (required, no default), token TTL,
                                 PASSWORD_HASH_ITERATIONS (env, default 600_000)
app/db.py                        engine, SessionLocal, get_session() dependency; NO create_all
app/orm.py                       SQLAlchemy 2.0 typed models: User + one class per entity
app/errors.py                    RULE_STATUS table, RuleViolation exception
app/auth.py                      hash/verify password, issue/decode JWT, current_user dependency
app/crud/__init__.py
app/crud/<entity>.py             list/get/create/update/delete with ownership + parent checks,
                                 generated guards and transitions, to_model()
app/routes/__init__.py
app/routes/auth.py               /auth/signup, /auth/login
app/routes/<entity>.py           thin routes: load → guards → hooks → crud → commit
app/rules/__init__.py            empty
app/rules/<entity>.py            coder-writable hook stubs (the ONLY coder-writable files)
app/main.py                      app wiring, exception handlers, GET /health
tests/conftest.py                read-only Postgres fixtures (see 1.6)
tests/generated/test_generated_<entity>.py   read-only generated tests (see 1.7)
tests/test_<entity>.py           written by the test writer (rules + actions only)
```

`<entity>` is `snake_case(EntityName)` (= `table_name()` in entities.py = the OpenAPI tag). One tag
per entity plus the tag `auth` (R2 necessary work).

### 1.2 Hook interface (R2 + R2 reopened, D5/D13)

In `app/rules/<entity>.py`, for an entity `Task` nested under `Project`:

```python
def before_create(session: Session, user: orm.User, data: models.TaskCreate,
                  parent: orm.Project) -> None: ...
def before_update(session: Session, user: orm.User, row: orm.Task, data: models.TaskUpdate,
                  parent: orm.Project) -> None: ...
def before_delete(session: Session, user: orm.User, row: orm.Task, parent: orm.Project) -> None: ...
def <action>(session: Session, user: orm.User, row: orm.Task, parent: orm.Project) -> orm.Task: ...
```

Top-level entities: same, without `parent`. Rules:

- A hook stub raises `NotImplementedError("<entity>.<hook>")` **only if** `doc.rules_for(entity, op)`
  is non-empty; otherwise its body is `return None`. Action stubs always raise
  `NotImplementedError` (their body is LLM work). main.py maps `NotImplementedError` → 501.
- Each stub's docstring lists every rule that applies to it: `id`, `status`, `text`.
- Hooks reject by `raise RuleViolation("<rule-id>", "human message")`. `app/errors.py` holds a
  generated `RULE_STATUS: dict[str, int]` built from the R11 rules; the exception handler looks the
  status up there, so the coder can only return documented statuses. An unknown id is a 500.
- Actions return the (possibly modified) ORM row; the route commits and converts it.

### 1.3 Route flow (generated, read-only)

For every operation, in this order:
1. `user = current_user` dependency (401 if missing/invalid; use `HTTPBearer(auto_error=False)`
   and raise 401 yourself, because FastAPI's default is 403).
2. Nested: `parent = crud.<parent>.get_owned(session, user, parent_id)` → 404 if missing or not
   visible. Item routes: `row = crud.<entity>.get(session, user, id[, parent])` → 404.
3. Generated guards for this op (R12): `if parent.archived == True: raise RuleViolation(...)`.
4. Generated transition check on update (R12): if the field is in the patch and
   `(old, new)` is not an allowed move and `old != new` → `RuleViolation` with status
   `TRANSITION_STATUS` (409). Add each transition rule id `"<entity>-<field>-transition"` to
   `RULE_STATUS`. Guard ids are added too.
5. Hook from `app/rules/<entity>.py`.
6. CRUD write, `session.commit()`, return `crud.<entity>.to_model(row)`.

Ownership: `owner: user` top-level rows have `owner_id` (FK users.id, ON DELETE CASCADE, indexed)
and every query filters `owner_id == user.id`. `owner: none` rows have no `owner_id`; any signed-in
user may read and write. Nested rows have `<parent>_id` (FK, ON DELETE CASCADE, indexed) and are
only reachable through an already-visible parent. Lists: `ORDER BY id LIMIT :limit OFFSET :offset`,
`limit` 1..100 default 50, `offset` ≥ 0 (R10); put the three numbers in `config.py`
(`list_limit_default`, `list_limit_max`) and pass them to the template.

### 1.4 Database and types

- Driver URL: `postgresql+psycopg://<user>:<password>@127.0.0.1:<port>/<db>`.
- Column types: `str` → `String(max)` or `Text` if no max; `int` → `BigInteger`; `float` →
  `Double`; `bool` → `Boolean`; `date` → `Date`; `datetime` → `DateTime(timezone=True)`;
  `enum` → `String(len(longest value))` (no Postgres enum type). `nullable = optional`. Python-side
  defaults only (no `server_default`), so `alembic check` stays simple. `users`: `id`, `email`
  (unique, String(254)), `password_hash` (String).
- IDs: `Integer` primary keys.

### 1.5 Contract plumbing that Schemathesis will otherwise break (decided in planning)

Add these in `expand()` (T3), not in prompts:
- Path params `id` and `<parent>_id`: `{"type": "integer", "minimum": 1, "maximum": 2147483647}`
  (int4 PK). Without it, huge ids raise a psycopg out-of-range error → 500.
- `int` fields without `min`/`max` get `minimum: -9223372036854775808`, `maximum:
  9223372036854775807` in the schema (BigInteger range).
- Every `str` field, and `Credentials.email`, gets `"pattern": "^[^\\u0000]*$"` (merged into the
  email pattern as `^[^@\\s\\u0000]+@[^@\\s\\u0000]+$`): Postgres text cannot store NUL bytes and
  Schemathesis generates them.
- If Schemathesis' `positive_data_acceptance` check flags documented 409s from rules, guards or
  transitions as failures, configure its expected statuses (Schemathesis 4 config file
  `schemathesis.toml` → `[checks.positive_data_acceptance] expected-statuses = [...]`) generated
  per project, rather than dropping the check. Verify at T9 time.

### 1.6 Test configuration (R9, OV#18, OV#19)

`tests/conftest.py` (v2, read-only):
- Reads `DATABASE_URL` and `JWT_SECRET` from the environment (the harness sets them; fail with a
  clear message if unset). Never runs migrations or create_all: the harness hands pytest a database
  already cloned from the migrated template.
- `autouse` per-test fixture: one connection, `SET lock_timeout = '5s'`, then one generated
  `TRUNCATE <every entity table>, users RESTART IDENTITY CASCADE`.
- `client` fixture: `TestClient(app)`.
- **The single auth test helper (R16):** a `make_user` fixture returning a callable
  `make_user(email: str | None = None) -> dict[str, str]` that signs up + logs in through the API
  and returns `{"Authorization": "Bearer ..."}` headers. Convenience fixtures `user_a`, `user_b`.
  Both the generated tests and the test writer use only these.
- `pytest.mark.rule("<id>")` marker registered in `pyproject.toml`
  (`[tool.pytest.ini_options] markers = ["rule(id): the entities rule this test covers"]`).
- Password hashing cost comes from `PASSWORD_HASH_ITERATIONS`; the harness sets `1000` for checks.

### 1.7 Generated tests (`tests/generated/`, read-only for both agents)

File names must differ from the test writer's (`test_generated_<entity>.py` vs `test_<entity>.py`)
because pytest's default import mode needs unique basenames. Per entity:
- 401 on every operation without a token.
- `owner: user`: user B gets 404 on user A's row for get/update/delete and **every action**
  (OV#15); B's list does not contain A's rows. For nested entities, B gets 404 on A's parent's
  collection.
- `owner: none`: B can read A's row.
- Create, then read back through the (nested) route; every field matches.
- Delete, then get → 404; deleting a parent removes its children.
- Pagination: `limit=101` → 422; `limit` and `offset` page correctly over 3 rows.
- One test per guard: set up the condition (e.g. PATCH the parent to `archived: true`), assert the
  guard's status; and the accepting side.
- One test per transition field: every allowed move succeeds; one disallowed move → 409.

Sample payloads come from a pure function `sample_value(spec: FieldSpec, n: int)` in
`factory/stations/scaffold_entities.py` (strings of `min` length padded with `n`, ints/floats at
`min` or 0 clamped into range, bools False, dates `2026-01-0<n>`, enums the default or first
value). Note: an LLM rule may legitimately reject a sample payload; keep the sample entities' rules
compatible with it (project-limit only triggers after 20 rows).

### 1.8 Gates under the entities profile (R4, R11, R13)

- Red check and cheat probe pass **only** the test writer's `tests/test_*.py` files to pytest.
  The main pytest check runs both folders.
- Static gate v2 (replaces the v1 gate for this profile):
  - every R11 rule id appears in at least one writer test as `@pytest.mark.rule("<id>")`;
  - every `actions:` operation is called by at least one writer test;
  - failures are grouped by entity (from entities.yaml, not path segments);
  - guards and transitions are **not** required (generated tests cover them).
- Cheat probe v2 renders two variants of `app/rules/`, each must make the writer suite fail:
  - `never_reject`: every hook `return None`; every action `return row`;
  - `always_reject`: every hook that has rules raises `RuleViolation(<its first rule id>, ...)`;
    hooks without rules return None; actions with rules raise, actions without rules return row.
  Both `CheckResult`s go into the iteration and the report.
- Test writer is skipped with reason "no rules or actions for the LLM" when `doc.llm_items() == 0`.

### 1.9 Budgets (R18)

`config.py` gets, per limit, `base`, `per_item` and `cap` (items = `doc.llm_items()`):

| limit | base | per item | cap |
|---|---|---|---|
| `max_iters` | 4 | 2 | 16 |
| `max_lines_changed_per_iter` | 150 | 100 | 600 |
| `max_cost_usd_per_run` | 1.0 | 0.75 | 10.0 |

`FactoryConfig.scaled_for(items: int) -> FactoryConfig` returns a copy with
`min(cap, base + per_item * items)` for each. Only the entities profile calls it. The computed
values are recorded in `RunRecord.budgets`.

### 1.10 Run identity (R1, OV#20)

- Entities runs: `run_id = f"{timestamp}-{doc.app}"`; `RunRecord.spec_name = doc.app` so
  `latest_passed_run` keys on the app.
- `RunRecord` gains: `profile: str`, `approved: bool | None`, `budgets: dict[str, float]`,
  `cheat_probes: list[CheckResult]`, `deploy: CheckResult | None` (T12). Defaults keep old run.json
  files loadable.

---

## 2. Order of work and PR boundaries (R17)

| PR | Tasks | Merge gate |
|---|---|---|
| PR 1: v2 core | T2 → T3 → T5 → T4 → T6 → T7 → T8 → T9 | `FACTORY_E2E=1` passes, including the new tasks scenarios |
| PR 2: spec_writer | T10 → T11 | scripted draft scenario passes; eval run once by the owner |
| PR 3: deploy + report | T12 → T13 | e2e reports deploy healthy (needs Docker) |

T5 does not depend on T4, so it can come before or in parallel. **R15 gate:** the design says T4
waits until the Assignment (a teammate reviewing a real description) is written into Open Question
1. The owner chose to proceed without it; T13 records that in the design doc.

---

## T2 — Entities schema: finish, fix, test

**Files:** `factory/entities.py`, `tests/test_entities.py` (new), `examples/tasks/entities.yaml`
(new), `examples/tasks/description.md` (new).

**Already done in entities.py:** `FieldSpec`, `RuleSpec`, `GuardSpec`, `ActionSpec`, `EntitySpec`,
`EntitiesDoc`, `_doc_problems` (owner required for top-level, nested may not declare owner, one
nesting level, reserved names, kebab-case rule ids, guard grammar, `auth` must be `jwt`),
`parse_entities`, `load_entities`.

**Build / fix:**
1. **YAML `on:` bug (real, found in planning).** PyYAML follows YAML 1.1 and loads an unquoted
   `on:` key as the boolean `True`, so every `rules:`/`guards:` entry fails with
   "on: Field required". Fix in `load_entities` with a `yaml.SafeLoader` subclass whose implicit
   bool resolver only accepts `true`/`false` (case-insensitive), not `on/off/yes/no`. Use the same
   loader everywhere entities YAML is parsed (pipeline `_detect_profile` too, via a shared
   `load_entities_raw(path)` helper). Add a test with unquoted `on:`.
2. Fix ruff: `SIM102` at lines ~114 and ~117 (merge nested ifs), `F541` at line ~659
   (`f"Updated."`).
3. Fix mypy: line ~612, `{route.parent_param: ...}` where `parent_param` is `str | None`
   (narrow it first).
4. `approved` must be a real boolean `true`. A missing key means not approved. Validation does not
   reject `approved: false`; the pipeline does (T8).
5. Write `examples/tasks/entities.yaml` (hand-written, `approved: true`, ≤ 80 lines):
   - `app: tasks`, `auth: jwt`;
   - `Project` (`owner: user`): `name` str 1..200, `archived` bool default false;
     rule `project-limit` ("A user may have at most 20 non-archived projects; creating the 21st
     returns 409", `on: [Project.create]`, 409); action `archive` (`text:` "Marks the project
     archived. Archiving an already archived project returns 409.", returns Project) with rule
     `already-archived` (`on: [Project.archive]`, 409);
   - `Task` (`parent: Project`): `title` str 1..200, `status` enum todo/doing/done default todo
     with `transitions: {todo: [doing], doing: [done]}`, `due` date optional;
     guard `archived-project-rejects-tasks` (`on: Task.create`, `when: parent.archived == true`,
     409);
   - `gaps: []`.
6. Write `examples/tasks/description.md`: a one-page prose description of the same backend (users
   sign up; projects private to their owner; at most 20 active projects; archive action; tasks with
   todo → doing → done; archived projects reject new tasks; optional due date). This is the input
   for T10/T11.

**Tests (`tests/test_entities.py`), table-driven, one row per rejection:**
missing owner on top-level; owner on nested; parent unknown; parent is itself nested; self-parent;
reserved entity name (`User`); non-PascalCase name; reserved field (`id`, `owner_id`,
`project_id` on a Task under Project); bad field name; min > max; min on bool; enum without values;
duplicate enum values; transitions on non-enum; transition to unknown value; transitions without
default; bad default type; default out of range; duplicate rule id across entities; bad rule id;
rule `on` with unknown entity / unknown op / malformed target; guard on another entity; guard with
`parent.` on top-level; guard on missing field; guard on a transitions field; guard on float; guard
literal of wrong type; malformed `when`; action named `create`; action returning another entity;
`auth: sso` → "not supported yet"; duplicate plural; unknown top-level key (extra=forbid).
Plus: the example file loads; `llm_items()` == 3 for the example (2 rules + 1 action); `ordered()` puts parents first;
`rules_for("Project", "create")` returns `project-limit`; unquoted `on:` works.

**Done when:** ruff and mypy clean; every row above has a test; the example loads.

---

## T3 — `expand()` to OpenAPI: harden and golden-test

**Files:** `factory/entities.py`, `tests/test_expand.py` (new),
`tests/fixtures/expand/tasks.openapi.yaml` (new golden file).

**Build:**
1. Apply section 1.5: id/parent-id bounds, int64 default bounds, NUL-free string pattern.
2. Make sure every list operation documents 422 (it does for `op == "list"`; keep a test).
3. Make sure each tag equals `table_name(entity)` and the `auth` tag exists (R2 necessary work).
4. Keep `links` from create → get and create → child create.
5. Add `write_golden` helper only inside the test (no regeneration code in `factory/`): the test
   compares `dump_openapi(expand(example))` to the golden file; on mismatch the failure message
   says how to regenerate (a one-line python command in the test docstring).

**Tests:**
- golden comparison for `examples/tasks/entities.yaml`;
- `lint_spec(expand(doc)).ok` is True (planning check: currently true with 49 warnings, all "no
  response example"; warnings are fine);
- `parse_spec(expand(doc))` yields tags `{auth, project, task}`;
- documented statuses: `createProject` has 409 (rule), `createTask` has 409 (guard), `updateTask`
  has 409 (transition), `archiveProject` has 409 and 404, every op except auth has 401;
- `TaskCreate` has no `status` property (transitions field), `TaskUpdate` has it;
- `due` is nullable in all three schemas and not required in Create;
- determinism: two calls give identical dumps.

**Done when:** tests pass, golden file committed, lint ok.

---

## T5 — `factory/harness/postgres.py`: one server per run, template DB, clone per check

**Files:** `factory/harness/postgres.py` (new), `factory/config.py`, `factory/harness/sandbox.py`
(passthrough vars), `tests/test_postgres.py` (new).

**Config (`config.py`):**
`postgres_backend: str = "docker"` (`"docker"` | `"local"`), `postgres_image: str =
"postgres:16-alpine"`, `postgres_bin_dir: str = "/usr/lib/postgresql/16/bin"`,
`postgres_ready_timeout_s: int = 60`. All overridable via `FACTORY_*` env.

**API:**
```python
class PostgresUnavailable(RuntimeError): ...        # Docker down / binaries missing → fail fast

@dataclass(frozen=True)
class PostgresServer:
    host: str            # always "127.0.0.1"
    port: int
    user: str            # "postgres"
    password: str        # secrets.token_urlsafe(24), per run
    admin: Callable[[str], sandbox.CommandResult]   # runs one SQL statement as superuser
    def url(self, db: str) -> str: ...               # postgresql+psycopg://...
    def create_database(self, name: str, template: str | None = None) -> None: ...
    def drop_database(self, name: str) -> None: ...

@contextmanager
def postgres_server(cfg: FactoryConfig, run_dir: Path, run_id: str) -> Iterator[PostgresServer]: ...

# pure command builders, unit-tested:
def docker_run_cmd(name: str, image: str) -> list[str]
def docker_port_cmd(name: str) -> list[str]
def docker_ready_cmd(name: str) -> list[str]
def docker_rm_cmd(name: str) -> list[str]
def docker_psql_cmd(name: str, sql: str) -> list[str]
def local_initdb_cmd(bin_dir: Path, data_dir: Path, pwfile: Path) -> list[str]
def local_postgres_cmd(bin_dir: Path, data_dir: Path, port: int) -> list[str]
def local_psql_cmd(bin_dir: Path, port: int, sql: str) -> list[str]
def safe_db_name(raw: str) -> str      # [a-z0-9_], ≤ 63 chars; SQL identifiers are never user text
```

**Docker backend:** `docker run -d --rm --name factory-pg-<run_id> -e POSTGRES_PASSWORD
-p 127.0.0.1::5432 <image>` (the password goes through the environment, not argv); host port from
`docker port`; readiness by looping `docker exec <name> pg_isready -U postgres` until
`postgres_ready_timeout_s`; admin SQL via `docker exec <name> psql -U postgres -v ON_ERROR_STOP=1
-c <sql>`; `docker rm -f <name>` in `finally`. A first `docker info` that fails →
`PostgresUnavailable("Docker is not reachable: ...")`.

**Local backend:** data dir `run_dir/postgres/data` (inside the project tree); password file
written with mode 0600 and deleted after initdb; `initdb -D <data> -U postgres
--auth=scram-sha-256 --pwfile=<file>`; start `postgres -D <data> -p <free port> -k ""
-c listen_addresses=127.0.0.1` as a child process (argv list, no shell: the project path has a
space, `pg_ctl -o "..."` breaks on it); readiness by polling `pg_isready -h 127.0.0.1 -p <port>`;
admin SQL via `psql -h 127.0.0.1 -p <port> -U postgres -v ON_ERROR_STOP=1 -c <sql>` with
`PGPASSWORD` in the env; `finally`: SIGTERM, wait, SIGKILL, `rmtree(data)`.

**Environment (OV#14):** add `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG` to the env used for
docker commands only (pass them via `extra` to `sandbox.run`; do not add them to
`PASSTHROUGH_ENV`, so generated code still gets the scrubbed env).

**Notes:** `CREATE DATABASE` cannot run in a transaction block: send one statement per `-c`.
`CREATE DATABASE x TEMPLATE t` fails if anything is connected to `t`; the harness never connects
to the template after migrating it.

**Tests (`tests/test_postgres.py`):**
- every command builder (exact argv, password never in argv, `127.0.0.1` bind);
- `safe_db_name` table;
- `postgres_server` with a fake `sandbox.run` (monkeypatch) that simulates `docker info` failing →
  `PostgresUnavailable`, and checks `docker rm -f` is called even when readiness times out;
- one opt-in real test, skipped unless `FACTORY_E2E=1`, that starts the configured backend,
  creates a template, clones it, and drops it.

**Done when:** unit tests pass; the opt-in test passes with `FACTORY_POSTGRES_BACKEND=local` in the
sandbox.

---

## T4 — Project template v2 (largest task; split into T4a–T4d sessions)

**Files:** `factory/stations/scaffold_entities.py` (new),
`factory/templates/project_v2/*.j2` (new), `tests/test_scaffold_entities.py` (new).
Reuse from `factory/stations/scaffold.py` without changing its behaviour: `generate_models`,
`harden_models`, `ruff_format`, `write_manifest`, `setup_env`, `file_hash`.

Entry point:
```python
def scaffold_entities(doc: EntitiesDoc, entities_path: Path, openapi: dict[str, Any],
                      project: Path, timeout_s: float, cfg: FactoryConfig) -> ScaffoldResult
def static_files_v2(doc: EntitiesDoc, spec: ApiSpec, cfg: FactoryConfig) -> dict[str, str]
```
It writes `openapi.yaml` (from `dump_openapi`), copies `entities.yaml`, renders every template,
runs `generate_models` on the written `openapi.yaml`, formats `.py` files with `ruff_format`, and
writes the manifest. A view-model builder `entity_view(doc, name) -> dict` (pure, tested) gives
templates everything they need: class name, table, plural, route, parent info, owner mode, columns
(name, SQLAlchemy type expr, nullable, python default), create/update model names, guards with
parsed `(on_parent, field, literal)`, transitions, rules per op, actions, sample payloads.

### T4a — Persistence: `pyproject.toml.j2`, `settings.py.j2`, `db.py.j2`, `orm.py.j2`,
`alembic.ini.j2`, `migrations/env.py.j2`, `migrations/versions/0001_initial.py.j2`, `errors.py.j2`

- pyproject deps: `fastapi`, `uvicorn`, `sqlalchemy>=2.0`, `pydantic>=2.7`, `alembic>=1.13`,
  `psycopg[binary]>=3.2`, `pyjwt>=2.8`; dev: `pytest`, `httpx`, `mypy`. mypy strict on `app` with
  `app.models` ignored (as v1). ruff excludes `app/models.py`, `client`, `tests`, `migrations`.
  pytest markers: `rule(id)`. No `email-validator`, no password-hash library (use stdlib, below).
- `settings.py`: read env at import; `JWT_SECRET` missing or empty → `raise RuntimeError("JWT_SECRET
  is not set ...")` (R5: the app refuses to start). `DATABASE_URL` required too. No defaults for
  either.
- `orm.py`: `class Base(DeclarativeBase)`, `User`, entity classes with `Mapped[...]`, FKs with
  `ondelete="CASCADE"`, `relationship` not needed.
- Migration: explicit `op.create_table(...)` for users then entities in `doc.ordered()` order;
  `op.create_index` on every `owner_id` and parent FK; `downgrade` drops in reverse.
- `migrations/env.py`: `target_metadata = orm.Base.metadata`, URL from `app.settings`.

**Test:** OV#11 — render the sample project into a tmp dir, create a database (opt-in, real
Postgres, `FACTORY_E2E=1`), run `alembic upgrade head` then `alembic check` with the project venv,
assert exit 0.

### T4b — Auth and CRUD: `auth.py.j2`, `crud/entity.py.j2`, `routes/auth.py.j2`

- Passwords: `hashlib.pbkdf2_hmac("sha256", pw, salt, iterations)`, stored as
  `pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>`; verify with `hmac.compare_digest`.
  Iterations from `settings.PASSWORD_HASH_ITERATIONS` (OV#18). bandit must stay clean.
- JWT: PyJWT HS256, `sub` = user id, `exp` = now + TTL. Decode errors → 401.
- `current_user(credentials = Depends(HTTPBearer(auto_error=False)), session = Depends(get_session))`
  → `orm.User`, else `HTTPException(401, headers={"WWW-Authenticate": "Bearer"})`.
- Signup: duplicate email → 409; returns `User` (201). Login: wrong email or password → 401.
- `crud/<entity>.py`: `get_owned` / `get`, `list_`, `create`, `update`, `delete`, `to_model`.
  `to_model` builds the Pydantic model explicitly field by field (no `from_attributes`).

### T4c — Wiring and hooks: `routes/entity.py.j2`, `rules/entity.py.j2`, `main.py.j2`,
`AGENTS.md.j2`, `cheat_rules_never.py.j2`, `cheat_rules_always.py.j2`, `.env.example.j2`

- Routes follow section 1.3 exactly; actions are `POST {item}/{action}` returning 200.
- `main.py`: include `auth` + every entity router; handlers for `NotImplementedError` (501),
  `RuleViolation` (`RULE_STATUS[id]`, `{"detail": message, "rule": id}`), the v1 400→422 and
  405 `Allow` plumbing (copy from `project/main.py.j2`); `GET /health` → `{"status": "ok"}`
  (excluded from the schema with `include_in_schema=False`). No `init_db`/`create_all`.
- `AGENTS.md` (project): layout table from 1.1 with owners; the coder writes only `app/rules/**`;
  how to raise `RuleViolation`; schema changes are entities changes (go back for approval);
  forbidden patterns including the DDL tripwire.
- Cheat templates per section 1.8.

### T4d — Tests: `conftest.py.j2`, `tests/generated/test_generated_entity.py.j2`

Section 1.6 and 1.7. The generated tests must pass against a correct implementation of the sample
(the scripted `tasks_v1` coder output in T9) and must not depend on any LLM rule beyond 1.7's note.

**T4 unit tests (`tests/test_scaffold_entities.py`, no Postgres needed):**
- `static_files_v2` on the sample produces exactly the expected file list;
- every generated `.py` file compiles (`compile(src, path, "exec")`);
- hook stubs: `before_create` of Project raises NotImplementedError (has `project-limit`);
  `before_update` of Project returns None; `archive` raises;
- `RULE_STATUS` contains `project-limit`, `already-archived`, the guard id and the transition id;
- `entity_view` columns/types table for every field type;
- the migration creates indexes on `owner_id` and `project_id`;
- `sample_value` table;
- cheat variants differ as described in 1.8.

**Done when:** unit tests pass; a manual `--no-llm` build of the sample (after T8) reaches the
checks (failures from NotImplementedError are expected there).

---

## T6 — `checks.py`: Postgres URL, per-run JWT secret, signup token for Schemathesis

**Files:** `factory/harness/checks.py`, `factory/harness/check_env.py` (new, small),
`tests/test_checks_parsing.py`, `tests/test_check_env.py` (new).

**Build:**
```python
@dataclass
class CheckEnv:                       # None under the openapi profile → v1 behaviour, unchanged
    server: PostgresServer
    template_db: str
    jwt_secret: str                   # secrets.token_urlsafe(32), made once per run (R5)
    hash_iterations: int = 1000
    def fresh_db(self, label: str) -> str: ...    # CREATE DATABASE <safe label> TEMPLATE <tmpl>
    def env_for(self, db: str) -> dict[str, str]:  # DATABASE_URL, JWT_SECRET, PASSWORD_HASH_ITERATIONS
```
- `run_pytest(..., env: Mapping[str, str] | None = None, test_paths: Sequence[str] = ())` —
  `test_paths` appended to argv (used by the red check / cheat probe for writer-only runs).
- `run_schemathesis(..., check_env: CheckEnv | None = None)`: v2 → fresh DB, server env from
  `env_for`, `health_path="/health"`; before `st run`, POST `/auth/signup` + `/auth/login` with a
  random email/password via httpx and pass `-H "Authorization: Bearer <token>"`. Never log the
  token or secret into check logs (redact the `-H` value in `_save_log`).
- `run_all(project, cfg, log_dir, check_env=None)`: v2 → one fresh DB for pytest, another for
  Schemathesis. v1 call sites unchanged.
- Pure helper `schemathesis_cmd(spec, url, cfg, report_path, auth_header: str | None) -> list[str]`
  so the command line is unit-testable.

**Tests:** `schemathesis_cmd` with and without auth; log redaction; `CheckEnv.env_for` keys;
`fresh_db` issues `CREATE DATABASE ... TEMPLATE ...` (fake server).

**Done when:** unit tests pass; v1 e2e unchanged.

---

## T7 — Gates v2

**Files:** `factory/harness/test_quality.py`, `factory/harness/guardrails.py` (no change expected
beyond lists), `factory/config.py`, `factory/stations/test_writer.py`,
`factory/harness/context.py`, `factory/templates/prompts/test_writer.*.j2` (new v2 variants:
`test_writer_v2.system.j2`, `test_writer_v2.user.j2`), `tests/test_test_quality.py`,
`tests/test_guardrails.py`, `tests/test_config.py`.

**Build:**
1. `config.for_profile("entities")` read-only list (OV#2), asserted by a test: `openapi.yaml`,
   `AGENTS.md`, `pyproject.toml`, `uv.lock`, `app/models.py`, `tests/conftest.py`, `client/**`,
   `entities.yaml`, `tests/generated/**`, `app/routes/**`, `app/main.py`, `app/crud/**`,
   `app/orm.py`, `app/auth.py`, `app/settings.py`, `app/db.py`, `app/errors.py`, `migrations/**`,
   `alembic.ini`, `Dockerfile`, `compose.yaml`. `coder_writable = ("app/rules/**",)`.
   `test_writer_unreadable` adds `app/rules/**`. DDL patterns already added in T1.
2. `assess_tests_v2(files, doc) -> CheckResult` (section 1.8). Extract rule markers with AST:
   decorators `pytest.mark.rule("<id>")` (string constant arg). Reuse `extract_tests` for calls.
   An action call matches `POST .../{x}/<action>` with `path_regex`.
3. `red_check(..., test_paths, env)` and `cheat_probe_v2(project, doc, cfg, log_dir, check_env)
   -> list[CheckResult]` (two results, names `cheat_never_reject` and `cheat_always_reject`). The
   probe copies the project like v1, replaces `app/rules/<entity>.py` from the cheat templates,
   uses a fresh DB per variant, runs only writer tests.
4. `test_writer.py`: if `cfg.profile == "entities"` use the v2 gates; `_gates` returns
   collect, static v2, red, both cheat results. Record the cheat results in
   `ctx.record.cheat_probes` (last attempt's).
5. Test writer v2 prompt: it writes `tests/test_<entity>.py` for entities with rules/actions only;
   every rule test carries `@pytest.mark.rule("<id>")`; test both the reject and accept side of
   every rule (explain the two cheat variants); use `make_user`/`user_a` fixtures; do not test CRUD,
   ownership, guards or transitions (generated tests do). Context: `entities.yaml`, `openapi.yaml`,
   `AGENTS.md`, `tests/conftest.py`, `tests/generated/*.py`, `app/models.py`.
6. Coder v2 prompt (`coder_v2.system.j2`, `coder_v2.user.j2`): writes only `app/rules/**`; the hook
   contract from 1.2; raise `RuleViolation`; never change schema; context includes
   `entities.yaml`, `app/orm.py`, `app/models.py`, `app/errors.py`, `app/crud/*.py`,
   `app/routes/*.py`, `app/rules/*.py`, all tests. `build_coder_context` picks the template by
   profile.

**Tests:** static gate v2 table (missing marker, marker with wrong id, action not called, all good);
config v2 list test; red/cheat selection passes `test_paths` (fake `run_pytest`); cheat template
rendering for the sample.

**Done when:** unit tests pass; the scripted trivial-suite rejection is proven in T9.

---

## T8 — Pipeline and CLI accept entities files

**Files:** `factory/pipeline.py`, `factory/cli.py`, `factory/run_context.py`,
`factory/report/models.py`, `tests/test_pipeline.py`, `tests/test_cli.py`.

**Build:**
1. Refusals, each a clear message recorded as a failed `lint` station (unit test per message):
   - entities file fails validation → every problem line in the detail;
   - `approved` not `true` → "entities.yaml is not approved: review it, then set `approved: true`"
     (R1/D18);
   - `--from` or `--incremental` with an entities file → "incremental builds are not supported for
     entities files yet (Open Question 2 in docs/designs/description-to-backend.md); run a full
     build" (R19). Do this check in `build()` before creating the run dir as well as in the CLI.
2. Replace the T1 "not supported yet" stop with the v2 flow:
   `lint` (validate, approve check, `expand`, `lint_spec` on the expansion) → `scaffold`
   (`scaffold_entities`) → `setup` (`uv sync`) → `postgres` (start server via an `ExitStack`
   that lives until the end of `_build`, create + migrate the template with
   `<venv>/bin/python -m alembic upgrade head`; failure → `StopRun`; Docker down → `StopRun`
   with the `PostgresUnavailable` message) → `test_writer` (or skip) → `baseline_checks` →
   `coder` → `contract` → `sdk` → (T12) `deploy_check`.
3. `RunContext` gains `entities: EntitiesDoc | None` and `check_env: CheckEnv | None`; the coder
   and test writer pass `ctx.check_env` to `checks.run_all` and the gates.
4. Budgets: `cfg = cfg.scaled_for(doc.llm_items())` for entities runs; record in
   `record.budgets`; `record.profile`, `record.approved`.
5. Run id / spec name per 1.10.
6. CLI help text: `build` accepts "OpenAPI 3.x spec or approved entities file".

**Tests:** each refusal; `scaled_for` table (0, 3, 50 items → 4/10/16 iterations, capped); run id uses the app name;
the openapi profile path is untouched (existing tests).

**Done when:** `factory build examples/tasks/entities.yaml --no-llm` runs every station (expected
to fail at checks because action stubs raise 501).

---

## T9 — Sample scenarios and e2e

**Files:** `tests/fixtures/scripted/tasks_v1/` (script + files), `tests/fixtures/scripted/
tasks_cheat/`, `tests/test_e2e.py`.

**Build:**
- `tasks_v1/script.yaml`:
  - test_writer attempt 1: writes a **rejection-only** suite (tests that the 21st project gets 409
    and archiving twice gets 409, never that valid input is accepted) → must be rejected by
    `cheat_always_reject` ("NOT caught"); attempt 2 (`expect:` the rejection text) writes the full
    suite `files/test_project.py` with `@pytest.mark.rule` markers on both sides.
  - coder iteration 1 (`expect: ["Not implemented"]`): tries to write `tests/generated/...`
    (blocked, `read_only` violation) and `app/crud/project.py` (blocked), then writes a buggy
    `app/rules/project.py` (limit off by one: allows 21); iteration 2 (`expect:` the failing test
    name) writes the correct `files/rules_project.py`.
- `tasks_cheat/script.yaml`: good suite; coder ships the never-reject rules twice → run `failed`.
- e2e tests (new, alongside the three existing ones, existing assertions untouched):
  - `test_tasks_build_passes`: status passed; contract passed; violations are exactly
    `{"read_only"}`; test writer iterations `[False, True]`; `cheat_probes` both caught;
    `record.budgets["max_iters"] == 10` (4 + 2 × 3 items).
  - `test_tasks_cheat_fails`: status failed; last coder pytest failed.
  - `test_tasks_unapproved_refused` and `test_tasks_from_refused` can be unit tests (T8).
- Set `FACTORY_POSTGRES_BACKEND` from the environment in the e2e (default docker).

**Done when:** `FACTORY_E2E=1 uv run pytest tests/test_e2e.py` passes all five e2e tests.
This is the PR 1 merge gate.

---

## T10 — `factory draft` (spec_writer)

**Files:** `factory/stations/spec_writer.py` (new), `factory/templates/prompts/
spec_writer.system.j2`, `spec_writer.user.j2` (new), `factory/cli.py`, `factory/config.py`
(`spec_writer_max_attempts: int = 3`), `tests/fixtures/scripted/tasks_draft/script.yaml`,
`tests/test_spec_writer.py` (new).

**Build (R6, D8, D24, R1/D18):**
```python
class DraftTooLarge(ValueError): ...      # message lists every file's size and the cap
@dataclass
class DraftResult:
    path: Path | None; attempts: int; errors: list[str]; usage: Usage; gaps: list[str]
def draft(docs: Sequence[Path], out: Path, cfg: FactoryConfig, provider: LLMProvider) -> DraftResult
def extract_yaml(text: str) -> str          # the ```yaml fenced block, else the whole text
def render_draft(doc_yaml: str) -> str      # header comment + `approved: false` as FIRST key
```
- Refuse before any model call if the summed characters exceed `context_max_total_chars`
  (never trim).
- One `provider.complete(system=..., messages=[UserMessage(user)], tools=(), agent="spec_writer",
  max_tokens=cfg.llm_max_output_tokens)` per attempt; check the providers accept an empty tool
  list (Anthropic: omit `tools`; OpenAI: omit `tools`) and fix them if not.
- Validate with `parse_entities` after removing any `approved` key the model wrote; on errors,
  re-ask with the previous YAML and the exact error lines, up to `spec_writer_max_attempts`.
- Write the file only when valid. The header comment says: "Drafted by API Factory from <files>.
  Review every entity, owner, rule and gap, then change `approved: false` to `approved: true`."
- The system prompt contains the full format reference (every key in `entities.py`, the guard
  grammar, `owner` must be explicit, one nesting level, use `gaps:` instead of inventing) and the
  sample `examples/tasks/entities.yaml` as an example. The user prompt has the documents and, on
  retries, the errors.
- CLI: `factory draft DOCS... -o PATH [--force]`; refuses to overwrite without `--force`; exit 0
  on success, 1 on validation failure after all attempts, 2 for oversize input or provider config.
- Prints usage/cost and the `gaps:` list.

**Tests:** oversize refusal (no provider call); `extract_yaml`; `render_draft` puts `approved:
false` first; scripted scenario `tasks_draft` (attempt 1 returns YAML missing `owner:` → attempt 2
`expect:` "top-level entities must declare owner" → valid) writes a file that `load_entities`
accepts with `approved == False`; exhausting attempts writes nothing.

**Done when:** tests pass; `factory build <drafted file>` is refused until approved.

---

## T11 — Opt-in live eval for the spec_writer prompt

**Files:** `tests/test_eval_spec_writer.py` (new), `pyproject.toml` (register marker `eval`).

Skipped unless `FACTORY_EVAL=1`. Uses `get_provider()` from the environment, drafts
`examples/tasks/description.md` into `tmp_path`, and asserts: validates; entities `{Project,
Task}`; `Task.parent == "Project"`; `Project.owner == "user"`; `Task.status` enum values
`{todo, doing, done}`; `gaps == []`. Prints cost. README: run before merging prompt changes.

**Done when:** skipped by default; the owner runs it once with a real key.

---

## T12 — Deploy check and report fields

**Files:** `factory/templates/project_v2/Dockerfile.j2`, `compose.yaml.j2`,
`factory/stations/contract.py`, `factory/config.py` (`deploy_timeout_s: int = 600`),
`factory/report/models.py`, `factory/templates/report/report.md.j2`, `tests/test_report.py`,
`tests/test_deploy.py` (new).

**Build (OV#13):**
- Dockerfile: `python:3.12-slim`, copy uv from `ghcr.io/astral-sh/uv` image, `uv sync --frozen
  --no-dev`, `CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0
  --port 8000"]`.
- compose: `db` (postgres:16-alpine, healthcheck `pg_isready`, password from `${POSTGRES_PASSWORD}`),
  `app` (build ., `depends_on: db: condition: service_healthy`, `DATABASE_URL` pointing at `db`,
  `JWT_SECRET: ${JWT_SECRET:?set JWT_SECRET}`, ports `127.0.0.1:${APP_PORT:-8000}:8000`);
  compose reads `.env` by default for humans.
- `run_deploy_check(project, cfg, run_dir, run_id) -> CheckResult`: project name
  `factory-<safe run id>`; env `JWT_SECRET`, `POSTGRES_PASSWORD` random, `APP_PORT` a free port
  (never the harness Postgres port); `docker compose -p <name> up -d --build --wait` under
  `deploy_timeout_s`; GET `/health` → 200; signup, login, create the first top-level entity with
  `sample_value`s, GET it back; `docker compose -p <name> down -v` in `finally`; logs from
  `docker compose logs` saved on failure. Secrets are never written to a `.env` under `runs/`.
  Docker unreachable → a failed result with a clear summary (not an exception).
- Pipeline: `deploy_check` station after `sdk` for the entities profile only.
- Report: show profile, approval (sha256 + approved), computed budgets, both cheat probe results,
  the deploy result.

**Tests:** compose/docker command builders; report renders the new sections (fake record);
an e2e assertion `run.deploy.passed` in `test_tasks_build_passes`, skipped when
`FACTORY_POSTGRES_BACKEND=local` (no Docker).

**Done when:** e2e reports deploy healthy on a machine with Docker.

---

## T13 — Docs

**Files:** `AGENTS.md`, `README.md`, `docs/designs/description-to-backend.md`, `TODOS.md`.

- AGENTS.md rule 2: "Deterministic first ... The LLM writes business logic and behaviour tests,
  and may draft the entities spec, only behind schema validation and human approval
  (`approved: true`)."
- README: v2 pipeline table (draft → approve → build stations incl. postgres and deploy_check),
  usage (`factory draft`, building `examples/tasks/entities.yaml`), Docker requirement,
  `FACTORY_POSTGRES_BACKEND=local`, the v2 guardrail table, roadmap phases 7–11 checked.
- Design doc: delete the dead line "**Cheat probe:** the hardcoded implementation also has to fail
  the ownership tests." (OV#4, superseded by R4); add a note under R15 that the owner chose to
  build Phase 8 before the Assignment, with the date; record the planning decisions from section
  1.5 of this plan.
- TODOS.md: keep T1 (migrations across versions); add any follow-ups found during T2–T12.

**Done when:** read-through; links resolve.

---

## 3. Known risks to watch

| Risk | Where | Mitigation |
|---|---|---|
| Schemathesis treats documented 409s from rules as `positive_data_acceptance` failures | T6/T9 | configure expected statuses per project (1.5) |
| Hypothesis generates NUL bytes / huge ints → Postgres errors → 500 | T3 | schema patterns and bounds (1.5) |
| FastAPI returns 403 for a missing bearer token | T4b | `HTTPBearer(auto_error=False)` + explicit 401 |
| Duplicate test module basenames | T4d | `test_generated_<entity>.py` |
| `CREATE DATABASE` in a transaction / on a busy template | T5 | one statement per psql call; never connect to the template after migrating |
| Project path contains a space | T5 | argv lists only; never `pg_ctl -o "<string>"` |
| PyYAML reads `on:` as `True` | T2 | custom loader (T2 step 1) |
| A generated sample payload is rejected by an LLM rule | T4d | keep sample rules compatible; documented |
| v1 regressions | all | new templates folder; existing e2e assertions unchanged |
