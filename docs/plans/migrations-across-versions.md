# Migrations across entities versions

Answers Open Question 2 of `docs/designs/description-to-backend.md` and lifts decision R19 (today
`--from` / `--incremental` is refused for entities files). Status: design, not yet built.

## Goal

`factory build entities.yaml --from <run>` (and `--incremental`) takes a previous run's project and
the new entities file and produces a project whose database can be **evolved in place**: one new
Alembic revision for the schema difference, applied on top of the old revisions, never a rewrite of
revision 1. The approved file remains the only source of the schema (R3); the coder still cannot
change it.

## Decisions

1. **The diff is computed from the two entities documents, not from the databases.** Old document =
   `<previous run>/project/entities.yaml`; new document = the input. Pure function
   `diff_entities(old, new) -> SchemaDiff`. Revisions are rendered from the diff by a template
   (deterministic), not by `alembic revision --autogenerate`; `alembic check` is the verification
   that the result agrees with the ORM.
2. **Renames are explicit.** `renamed_from: OldName` on an entity and `renamed_from: old_field` on a
   field. Without it, a missing name plus a new name is a drop and an add. (Format change: one
   optional key on `EntitySpec` and `FieldSpec`; the key is rejected if the old name does not exist
   in the previous document or collides with a surviving name.)
3. **Every change is classified:**
   - *safe* (applied silently): add an entity; add a field that is `optional` or has a `default`;
     widen a string `max`; add an enum value (the column may grow); make a column nullable;
     add/remove `rules`, `guards`, `actions`, `transitions` (code only, no schema effect);
     rename (data preserved).
   - *destructive* (needs `--allow-destructive`; data may be lost): drop an entity, drop a field,
     narrow a string `max`, remove an enum value that rows may use.
   - *refused* (the file must change or a human writes a data migration): add a required field
     without a default (existing rows would violate it), change a field's `type`, make a nullable
     column required, change `owner` or `parent`.
   Refusals and the destructive flag are reported as lint-time errors naming the entity and field.
4. **Added required-with-default columns backfill correctly:** `add_column(..., nullable=False,
   server_default=<literal>)` then `alter_column(..., server_default=None)`, so existing rows get
   the default and the ORM (Python-side defaults only) still agrees with the database.
5. **The revision chain is stored in the project, which the next run copies.** Revisions are
   `migrations/versions/NNNN_<slug>.py` with `down_revision` the previous head; `NNNN` is the
   previous number plus one. The previous run's revision files are copied verbatim and treated as
   read-only history (their hashes are in the manifest); only the new revision is generated.
6. **Incremental scaffold for entities:** copy the previous project (same ignore rules as v1);
   regenerate every generated file from the new document **except** `migrations/versions/*` (history
   is kept, one revision added) and `app/rules/*` and the writer's `tests/test_*.py` (agent work is
   kept; stubs for new hooks are appended, as the v1 incremental scaffold does for services).
7. **The harness proves the evolution, not just the end state.** The template database is built by
   `alembic upgrade <previous head>` and then `alembic upgrade head`, so the new revision really runs
   on top of the old schema. A dedicated end-to-end test also seeds rows at the old head and checks
   they survive, that `alembic check` reports no drift, and that `downgrade -1` works.
8. **Agents:** the test writer runs only if the set of rules or actions changed (ids, `on`, text);
   otherwise it is skipped with that reason. The coder always starts from the baseline checks and is
   told what changed (a plain list of entity, field and rule changes, like v1's spec changes).
9. **Report:** `run.json` and `report.md` list the schema changes with their classification and the
   revision file that was generated.

## Out of scope

Data migrations that transform values; changing a column's type; moving an entity to a different
parent or owner; squashing revisions; applying migrations to someone's production database (the
generated container applies `alembic upgrade head` at start, as before).

## Build order

1. `renamed_from` in the format (+ validation) and `diff_entities` with the classification (pure,
   table-tested).
2. Revision rendering (template + `render_revision`), tested by running it for real against Postgres.
3. Incremental scaffold for entities (copy, regenerate, preserve, append stubs).
4. Pipeline and CLI: lift the refusal, the two-step template migration, the changed-rules logic for
   the test writer, the report section.
5. End-to-end: evolve the tasks sample (add a field, add an entity, rename a field), assert the
   seeded data survives and every gate still passes; tests for each refusal and the destructive flag.
