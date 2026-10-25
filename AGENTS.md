# AGENTS.md — rules for AI assistants working on the factory

This repo is **the factory** (the tool). It is written by humans, possibly with AI help. The code the
factory *produces* lives in `runs/<run_id>/project/` and has its own `AGENTS.md`, rendered from
`factory/templates/`. Do not confuse the two.

## Hard rules
1. **Two codebases, never mixed.** Never edit anything under `runs/` by hand, and never import from a
   generated project into `factory/`. If generated output is wrong, fix the factory (templates,
   prompts, harness) and re-run.
2. **Deterministic first.** Anything that can be generated mechanically (Pydantic models, route stubs,
   SDK, project skeleton) must use deterministic tools or Jinja2 templates, never the LLM. The LLM only
   writes business logic and behaviour tests.
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

## Before you finish a change
```bash
uv run ruff check .
uv run mypy
uv run pytest
FACTORY_E2E=1 uv run pytest tests/test_e2e.py   # when touching stations, templates or the harness
```
