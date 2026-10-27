# TODOS

## Factory v2

### Migrations across entities versions

**What:** Let `factory build entities.yaml --from <run>` evolve an existing database: emit a new Alembic revision for the entities diff instead of regenerating the initial one.

**Why:** Until this exists, v2 incremental builds are refused (decision R19 in docs/designs/description-to-backend.md), so every change to an entities file means a fresh build and a fresh database.

**Context:** Under R3 the ORM models and the initial Alembic revision are generated read-only from entities.yaml. The incremental machinery (factory/stations/incremental.py, `.factory/manifest.json`) regenerates untouched generated files, which would rewrite revision 1 in place and break any database already at it. Start from Open Question 2 in the design doc: decide how renames are told apart from drop-and-add (an explicit `renamed_from:` field is one option), whether Alembic autogenerate against the previous run's ORM is deterministic enough, and how the generated revision chain is stored in the manifest. Then lift the R19 refusal.

**Effort:** L
**Priority:** P2
**Depends on:** nothing now (the v2 core, entities format, generated ORM and migrations, and gates are built)

### Run the Docker paths on a machine with Docker

**What:** Run `FACTORY_E2E=1 uv run pytest tests/test_postgres.py tests/test_e2e.py` with the default `FACTORY_POSTGRES_BACKEND=docker`, and fix whatever it finds.

**Why:** The Docker Postgres backend (`factory/harness/postgres.py`) and the `docker compose` deploy check (`run_deploy_check` in `factory/stations/contract.py`, plus the generated `Dockerfile` and `compose.yaml`) were written and unit-tested against a fake Docker, but never run: the environment they were built in had no usable Docker daemon. Everything else was verified against a real Postgres through the `local` backend. Only `docker compose config` was run on the generated compose file.

**Context:** Things most likely to need a tweak on first contact: the Dockerfile's `uv sync --no-dev` needs the network at build time and a `uv.lock` (created by the setup station); the app container's health check; `docker run -p 127.0.0.1::5432` port parsing on Docker Desktop; the 600 s `deploy_timeout_s`.

**Effort:** S
**Priority:** P1
**Depends on:** a machine with Docker

### Run the live spec-writer eval and tune the prompt

**What:** `FACTORY_EVAL=1 uv run pytest tests/test_eval_spec_writer.py -s` with a real provider and key; adjust `factory/templates/prompts/spec_writer.*.j2` until a real model drafts the tasks backend correctly.

**Why:** Every other test replaces the model with a script, so nothing yet shows that the prompt makes a real model read a description correctly. The same goes for the test-writer and coder v2 prompts, which have only run against scripts: the first real build (`factory build examples/tasks/entities.yaml` with a real provider) is the proof.

**Effort:** S
**Priority:** P1
**Depends on:** an API key

### Show the format to a real engineer (the Assignment)

**What:** Write a one-page description of a backend the team actually needs, hand-write the `entities.yaml` you would expect, and ask a backend engineer: "Would you approve this? What's missing?" Record the answers in Open Question 1 of `docs/designs/description-to-backend.md`.

**Why:** The design gated Phase 8 on this (R15). The templates were built before it happened, on the owner's decision. The format only covers owner/parent isolation, enums with transitions, single-comparison guards, structured rules and actions; many-to-many relations, roles beyond owner, unique constraints across fields and computed fields are unknown needs.

**Effort:** M
**Priority:** P1
**Depends on:** a named engineer

### Add response examples to the expanded OpenAPI

**What:** Have `expand()` emit an example for each response body.

**Why:** Every entities build reports ~49 "no response example" lint warnings (harmless, but noise in the lint station).

**Effort:** S
**Priority:** P3

### Tie a rule id to the operations it is declared on

**What:** Make the generated `RuleViolation` handler reject (with a 500 naming the mistake) a hook that raises a rule id not declared for the operation being served.

**Why:** Today a hook may raise any declared id, so it could return a status that the OpenAPI does not document for that operation, which Schemathesis would flag as a contract failure but without saying why.

**Effort:** S
**Priority:** P3

### Look at the web UI in a real browser

**What:** Run `uv run factory ui` and click through the dashboard, a draft review, an approve and a build, including Cancel and the downloads, in a real browser.

**Why:** The UI was built and tested with an HTTP test client and a real build subprocess, but no browser was available, so layout, the strict CSP (no inline styles or scripts) and the meta refresh were never seen rendered.

**Effort:** S
**Priority:** P2

## Completed
