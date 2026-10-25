# API Factory

A proof-of-concept **software factory**: give it an OpenAPI 3.x spec, and it produces a working,
verified FastAPI backend.

```bash
uv run factory build specs/orders.yaml
```

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

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

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
| `FACTORY_<FIELD>` | override any scalar in `factory/config.py`, e.g. `FACTORY_MAX_ITERS=4`, `FACTORY_MAX_COST_USD_PER_RUN=1` |

## Usage

```bash
uv run factory llm-ping                                   # one tool-calling request; nothing executed
uv run factory build specs/orders.yaml                    # full build
uv run factory build specs/orders.yaml --no-llm           # deterministic stations only
uv run factory build specs/orders_v2.yaml --from <run_id> # incremental build on a previous run
uv run factory build specs/orders.yaml --incremental      # ...on the latest passed run of this spec
uv run factory runs                                       # list runs
uv run factory report latest                              # print report.md (--json for run.json)
```

`build` exits 0 only when the run status is `passed`.

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
FACTORY_E2E=1 uv run pytest tests/test_e2e.py           # end-to-end builds (~5 min)
```

See [AGENTS.md](AGENTS.md) for the rules for anyone (human or AI) changing the factory.

## Layout

```
factory/
  cli.py            build, report, runs, llm-ping
  config.py         budgets, limits, allowlists, model prices
  pipeline.py       orchestrates the stations
  spec.py           typed view of the OpenAPI spec shared by all stations
  run_context.py    per-run state
  stations/         lint, scaffold, test_writer, coder, contract, sdk, incremental
  harness/          context, tools, guardrails, loop, sandbox, checks, test_quality
  llm/              base interface, anthropic, openai_compat (OpenAI + DeepSeek), scripted, pricing
  report/           run record model + run.json / report.md writer
  templates/        project/ (generated code), prompts/ (agent prompts), report/
specs/              orders.yaml (v1.0), orders_v2.yaml (v1.1: note + cancel)
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
- [ ] Later: Docker sandbox with no network, GitHub Actions trigger, review agent, benchmark mode
