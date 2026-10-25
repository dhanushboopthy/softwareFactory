# TODOS

## Factory v2

### Migrations across entities versions

**What:** Let `factory build entities.yaml --from <run>` evolve an existing database: emit a new Alembic revision for the entities diff instead of regenerating the initial one.

**Why:** Until this exists, v2 incremental builds are refused (decision R19 in docs/designs/description-to-backend.md), so every change to an entities file means a fresh build and a fresh database.

**Context:** Under R3 the ORM models and the initial Alembic revision are generated read-only from entities.yaml. The incremental machinery (factory/stations/incremental.py, `.factory/manifest.json`) regenerates untouched generated files, which would rewrite revision 1 in place and break any database already at it. Start from Open Question 2 in the design doc: decide how renames are told apart from drop-and-add (an explicit `renamed_from:` field is one option), whether Alembic autogenerate against the previous run's ORM is deterministic enough, and how the generated revision chain is stored in the manifest. Then lift the R19 refusal.

**Effort:** L
**Priority:** P2
**Depends on:** PR 1 (v2 core: entities format, generated ORM and migrations, gates)

## Completed
