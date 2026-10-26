# AGENTS.md — rules for AI assistants working on the factory

This repo is **the factory** (the tool). It is written by humans, possibly with AI help. The code the
factory *produces* lives in `runs/<run_id>/project/` and has its own `AGENTS.md`, rendered from
`factory/templates/`. Do not confuse the two.

## Hard rules
1. **Two codebases, never mixed.** Never edit anything under `runs/` by hand, and never import from a
   generated project into `factory/`. If generated output is wrong, fix the factory (templates,
   prompts, harness) and re-run.
2. **Deterministic first.** Anything that can be generated mechanically (Pydantic models, route stubs,
   SDK, project skeleton; from an entities file also the OpenAPI spec, ORM, migrations, auth, CRUD
   with ownership, guards, state transitions and the ownership/CRUD tests) must use deterministic
   tools or Jinja2 templates, never the LLM. The LLM writes business logic and behaviour tests, and
   may draft the entities spec, but only behind schema validation and human approval
   (`approved: true`, which `factory build` checks).
3. **Verification the model cannot fake.** Loop exit conditions are always based on real command output
   (pytest, ruff, mypy, bandit, schemathesis exit codes and parsed reports), never on what the model
   says.
4. **Prompts are files.** Every prompt sent to an LLM is a Jinja2 template under
   `factory/templates/prompts/`. No prompt strings in Python code.
5. **No agent frameworks.** No LangChain, CrewAI, etc. The loop in `factory/stations/coder.py` is written
   by hand so the harness stays visible.
6. **Python only.** No JavaScript/TypeScript or Node-based tooling anywhere, including generated code.
7. **No secrets in code.** API keys come from the environment (`.env`, see `.env.example`).

## Code conventions
- Python 3.12, managed with `uv`. Add dependencies with `uv add` (or `uv add --dev`), never by editing
  the lock file.
- Small, typed, pure functions where possible. `mypy --strict` must pass on `factory/` and `tests/`.
- Guardrail limits, allowlists and budgets live only in `factory/config.py`.
- Harness code (guardrails, checks parsing, context building, sandbox) must have unit tests in `tests/`.
- Guardrail violations are recorded as data in the run report, not just raised.
- When generated code violates the spec because of a framework default (FastAPI, Pydantic),
  fix it deterministically in `factory/templates/project/` or `harden_models`, not in prompts.
- `tests/fixtures/` holds generated-project code for the scripted scenarios; it is excluded from
  the factory's ruff/mypy/pytest. Keep the scripts' `expect:` assertions meaningful.
- The input document picks a rule profile (`config.PROFILES`): an OpenAPI file runs `openapi`, an
  entities file runs `entities`. The `openapi` profile must not change behaviour; entities-only
  templates live in `factory/templates/project_v2/`, never in `templates/project/`.
- Load an entities file with `load_entities` / `load_entities_raw` / `load_entities_text`, never
  `yaml.safe_load`: PyYAML reads an unquoted `on:` key as `True`.
- Anything in a generated project that the coder must not change is listed in
  `config.ENTITIES_READ_ONLY`; the coder writes only `app/rules/**`.

## Before you finish a change
```bash
uv run ruff check .
uv run mypy
uv run pytest
FACTORY_E2E=1 uv run pytest tests/test_e2e.py   # when touching stations, templates or the harness
```
The end-to-end tests run a real Postgres and, by default, Docker. Without Docker use
`FACTORY_POSTGRES_BACKEND=local` (needs the PostgreSQL binaries; see `postgres_bin_dir`), which also
skips the Docker deploy check. `FACTORY_EVAL=1 uv run pytest tests/test_eval_spec_writer.py -s`
runs the live spec-writer eval; do it before merging a change to `spec_writer.*.j2` (needs an API key).
