"""Orchestrates the stations.

OpenAPI input:  lint → scaffold → setup → test writer → coder → contract → SDK.
Entities input: lint (validate, approval, expand) → scaffold → setup → postgres → test writer
                (skipped when the LLM has nothing to do) → coder → contract → SDK.

The run record (and report) is saved after every station and every agent iteration.
"""

from __future__ import annotations

import hashlib
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, EntitiesError, expand, load_entities
from factory.harness import checks, sandbox
from factory.harness.check_env import CheckEnv
from factory.harness.guardrails import Guardrails
from factory.harness.loop import Budget, BudgetExceeded
from factory.harness.postgres import PostgresUnavailable, postgres_server
from factory.llm.base import LLMProvider
from factory.report.models import IncrementalInfo, RunRecord, StationResult, now
from factory.report.writer import load_record
from factory.run_context import RunContext
from factory.spec import load_raw, load_spec, parse_spec
from factory.stations import contract, incremental, lint, scaffold, sdk
from factory.stations.coder import run_coder
from factory.stations.scaffold_entities import scaffold_entities
from factory.stations.test_writer import run_test_writer, skip_reason

TEMPLATE_DB = "template_db"
NOT_APPROVED = ("the entities file is not approved: review it, then set `approved: true` "
                "(R1, docs/designs/description-to-backend.md)")
INCREMENTAL_REFUSAL = (
    "incremental builds are not supported for entities files yet (Open Question 2 in "
    "docs/designs/description-to-backend.md); run a full build without --from / --incremental")


@dataclass
class BuildOptions:
    spec_path: Path
    use_llm: bool = True
    previous_run: str | None = None
    run_id: str | None = None


class StopRun(Exception):
    """A station failed in a way that makes later stations pointless."""


class IncrementalNotSupported(ValueError):
    """`--from` / `--incremental` was asked for an entities file (decision R19)."""


EMPTY_SPEC = parse_spec({"paths": {}})  # placeholder until lint has accepted the real spec


def input_profile(raw: Mapping[str, Any]) -> str:
    """The rule profile an input document runs under (see config.PROFILES)."""
    if "openapi" in raw:
        return "openapi"
    if "entities" in raw:
        return "entities"
    raise ValueError("input has neither a top-level `openapi` nor a top-level `entities` key")


def detect_profile(spec_path: Path) -> str:
    try:
        return input_profile(load_raw(spec_path))
    except (OSError, ValueError, yaml.YAMLError):
        return "openapi"  # unreadable or unrecognised input: lint reports the problem


def new_run_id(spec_path: Path, name: str | None = None) -> str:
    """`<timestamp>-<name>`; entities runs use the `app:` value, not the file name (OV#20)."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{name or spec_path.stem}"


def list_runs(cfg: FactoryConfig) -> list[RunRecord]:
    records = []
    if cfg.runs_dir.exists():
        for d in sorted(cfg.runs_dir.iterdir()):
            if (d / "run.json").exists():
                records.append(load_record(d))
    return records


def latest_passed_run(cfg: FactoryConfig, spec_name: str) -> str | None:
    passed = [r for r in list_runs(cfg) if r.spec_name == spec_name and r.status == "passed"]
    return passed[-1].run_id if passed else None


@contextmanager
def station(ctx: RunContext, name: str) -> Iterator[StationResult]:
    result = StationResult(name=name, status="passed")
    ctx.record.stations.append(result)
    ctx.log(f"[{name}]")
    try:
        yield result
    except (StopRun, BudgetExceeded):
        if result.status == "passed":
            result.status = "failed"
        raise
    except Exception as exc:
        result.status = "error"
        result.detail = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result.ended_at = now()
        ctx.log(f"  → {result.status}{': ' + result.detail if result.detail else ''}")
        ctx.save()


def skip(ctx: RunContext, name: str, why: str) -> None:
    ctx.record.stations.append(StationResult(name=name, status="skipped", detail=why,
                                             ended_at=now()))
    ctx.save()


def build(
    opts: BuildOptions, cfg: FactoryConfig, provider: LLMProvider | None,
    log: Callable[[str], None] = lambda _: None,
) -> RunRecord:
    spec_path = opts.spec_path.resolve()
    profile = detect_profile(spec_path)
    if profile == "entities" and opts.previous_run:
        raise IncrementalNotSupported(INCREMENTAL_REFUSAL)  # before any run directory exists
    cfg = cfg.for_profile(profile)
    doc: EntitiesDoc | None = None
    doc_errors: list[str] = []
    if profile == "entities":
        try:
            doc = load_entities(spec_path)
            cfg = cfg.scaled_for(doc.llm_items())  # budgets follow the LLM's workload (R18)
        except EntitiesError as exc:
            doc_errors = exc.problems
    run_id = opts.run_id or new_run_id(spec_path, doc.app if doc else None)
    run_dir = (cfg.runs_dir / run_id).resolve()
    if run_dir.exists():
        raise FileExistsError(f"run directory {run_dir} already exists")
    project = run_dir / "project"
    record = RunRecord(
        run_id=run_id,
        spec_name=doc.app if doc else spec_path.name,  # latest_passed_run keys on this (OV#20)
        spec_path=str(spec_path),
        spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        provider=provider.name if provider and opts.use_llm else "none",
        model=provider.model if provider and opts.use_llm else "none",
        profile=profile,
        approved=doc.approved if doc else None,
        budgets=({"llm_items": float(doc.llm_items()), "max_iters": float(cfg.max_iters),
                  "max_lines_changed_per_iter": float(cfg.max_lines_changed_per_iter),
                  "max_cost_usd_per_run": cfg.max_cost_usd_per_run} if doc else {}),
        config=cfg.to_dict(),
    )
    ctx = RunContext(
        cfg=cfg, spec=EMPTY_SPEC, project=project,
        run_dir=run_dir, record=record, provider=provider if opts.use_llm else None,
        guard=Guardrails(cfg, run_dir), budget=Budget(cfg, record.usage), log=log,
        entities=doc,
    )
    log(f"run {run_id} → {run_dir}")
    try:
        if profile == "entities":
            _build_entities(ctx, spec_path, doc_errors)
        else:
            _build_openapi(ctx, opts, spec_path)
    except StopRun as exc:
        record.status, record.status_reason = "failed", str(exc)
    except BudgetExceeded as exc:
        record.status, record.status_reason = "budget_exhausted", str(exc)
    except Exception as exc:
        record.status, record.status_reason = "error", f"{type(exc).__name__}: {exc}"
        record.error = record.status_reason
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "error.log").write_text(traceback.format_exc())
    finally:
        record.ended_at = now()
        ctx.save()
    log(f"status: {record.status}{' — ' + record.status_reason if record.status_reason else ''}")
    return record


def _setup_station(ctx: RunContext) -> None:
    """The generated project's own virtualenv (it never shares the factory's environment)."""
    with station(ctx, "setup") as st:
        env = scaffold.setup_env(ctx.project, ctx.cfg.setup_timeout_s)
        st.detail = f"uv sync in {env.duration_s}s"
        if not env.ok:
            st.detail = env.output[-1000:]
            raise StopRun("could not create the project environment")


def _final_status(ctx: RunContext, coder_status: str | None, coder_reason: str) -> None:
    record = ctx.record
    failed = [s.name for s in record.stations if s.status in ("failed", "error")
              and s.name != "baseline_checks"]
    if coder_status == "budget_exhausted":
        record.status, record.status_reason = "budget_exhausted", coder_reason
    elif failed:
        record.status = "failed"
        record.status_reason = f"failed stations: {', '.join(failed)}"
        if ctx.provider is None:
            record.status_reason += " (expected without an implementation: stubs return 501)"
    else:
        record.status = "passed"


def _build_entities(ctx: RunContext, spec_path: Path, doc_errors: list[str]) -> None:
    cfg, record, project = ctx.cfg, ctx.record, ctx.project
    ctx.run_dir.mkdir(parents=True)

    # 1. Lint: the file must be valid and approved, and its expansion must pass the OpenAPI lint
    #    (the generated spec goes through the same rules as a hand-written one).
    with station(ctx, "lint") as st:
        doc = ctx.entities
        if doc is None:
            st.detail = "\n".join(doc_errors)
            raise StopRun("the entities file is invalid")
        if not doc.approved:
            st.detail = NOT_APPROVED
            raise StopRun(NOT_APPROVED)
        openapi = expand(doc, cfg)
        result = lint.lint_spec(openapi)
        st.detail = ("; ".join(result.errors) if result.errors
                     else f"expanded to {len(openapi['paths'])} paths"
                          f" ({len(result.warnings)} spec warnings)")
        if not result.ok:
            raise StopRun("the spec expanded from the entities file failed lint")
        ctx.spec = parse_spec(openapi)

    # 2. Scaffold (deterministic: ORM, migration, auth, CRUD, routes, hooks, generated tests)
    with station(ctx, "scaffold") as st:
        res = scaffold_entities(doc, spec_path, project, cfg.setup_timeout_s, cfg)
        if not res.models.ok:
            st.detail = res.models.output[-1000:]
            raise StopRun("model generation failed")
        st.detail = f"{len(res.files)} files generated"
    ctx.guard = Guardrails(cfg, project)

    _setup_station(ctx)

    # The database server lives until the end of the run and is always removed.
    with ExitStack() as stack:
        # 3. Postgres: one server, a template database migrated by the real Alembic revision
        with station(ctx, "postgres") as st:
            try:
                server = stack.enter_context(postgres_server(cfg, ctx.run_dir, record.run_id))
            except PostgresUnavailable as exc:
                st.detail = str(exc)
                raise StopRun("Postgres is not available") from exc
            env = CheckEnv(server, TEMPLATE_DB)
            server.create_database(TEMPLATE_DB)
            migrated = sandbox.run(
                [str(project / ".venv" / "bin" / "python"), "-m", "alembic", "upgrade", "head"],
                cwd=project, timeout_s=cfg.command_timeout_s, env=env.env_for(TEMPLATE_DB))
            if not migrated.ok:
                st.detail = migrated.output[-1000:]
                raise StopRun("`alembic upgrade head` failed")
            ctx.check_env = env
            st.detail = (f"{cfg.postgres_backend} Postgres on 127.0.0.1:{server.port}; "
                         "schema migrated")

        # 4 + 5. Agents
        coder_status, coder_reason = None, ""
        if ctx.provider is None:
            skip(ctx, "test_writer", "--no-llm")
        elif (why := skip_reason(ctx)) is not None:
            skip(ctx, "test_writer", why)
        else:
            with station(ctx, "test_writer") as st:
                st.status, st.detail = run_test_writer(ctx)
                if st.status != "passed":
                    raise StopRun("the test writer's suite was rejected by the quality gates")
        with station(ctx, "baseline_checks") as st:
            baseline = checks.run_all(project, cfg, ctx.run_dir / "checks" / "baseline",
                                      check_env=ctx.check_env)
            st.checks = baseline
            st.status = "passed" if all(c.passed for c in baseline) else "failed"
            st.detail = " · ".join(f"{c.name}: {c.summary}" for c in baseline)
        if ctx.provider is None:
            skip(ctx, "coder", "--no-llm")
        else:
            with station(ctx, "coder") as st:
                coder_status, coder_reason = run_coder(ctx, baseline)
                st.status = "passed" if coder_status == "passed" else "failed"
                st.detail = coder_reason

        # 6. Contract: Schemathesis with a signed-up user's token, on a fresh cloned database
        with station(ctx, "contract") as st:
            record.contract = contract.run_contract(project, cfg, ctx.run_dir,
                                                    check_env=ctx.check_env)
            st.checks = [record.contract]
            st.status = "passed" if record.contract.passed else "failed"
            st.detail = record.contract.summary

        # 7. SDK
        with station(ctx, "sdk") as st:
            generated = sdk.generate_sdk(project, cfg.command_timeout_s)
            st.status = "passed" if generated.ok else "failed"
            st.detail = ("client/ generated and importable" if generated.ok
                         else generated.output[-500:])

    # 8. Deploy check: does the generated project come up with `docker compose` and work?
    if not cfg.deploy_check:
        skip(ctx, "deploy_check", "disabled (deploy_check is off, e.g. FACTORY_DEPLOY_CHECK=0)")
    else:
        with station(ctx, "deploy_check") as st:
            record.deploy = contract.run_deploy_check(project, cfg, ctx.run_dir, record.run_id,
                                                      doc)
            st.checks = [record.deploy]
            st.status = "passed" if record.deploy.passed else "failed"
            st.detail = record.deploy.summary

    _final_status(ctx, coder_status, coder_reason)


def _build_openapi(ctx: RunContext, opts: BuildOptions, spec_path: Path) -> None:
    cfg, record, project = ctx.cfg, ctx.record, ctx.project
    ctx.run_dir.mkdir(parents=True)

    # 1. Lint
    with station(ctx, "lint") as st:
        result = lint.lint_spec(load_raw(spec_path))
        st.detail = "; ".join(result.errors + result.warnings)
        if not result.ok:
            raise StopRun("spec lint failed")
    ctx.spec = load_spec(spec_path)

    # 2. Scaffold (fresh or incremental)
    diff: incremental.SpecDiff | None = None
    prev_project: Path | None = None
    with station(ctx, "scaffold") as st:
        if opts.previous_run:
            prev_project = cfg.runs_dir.resolve() / opts.previous_run / "project"
            if not prev_project.exists():
                raise StopRun(f"previous run {opts.previous_run} has no project")
            diff = incremental.diff_specs(load_spec(prev_project / "openapi.yaml"), ctx.spec)
            inc = incremental.incremental_scaffold(prev_project, project, ctx.spec, spec_path,
                                                   diff, cfg.setup_timeout_s)
            ctx.spec_changes = diff.describe()
            record.incremental = IncrementalInfo(
                previous_run=opts.previous_run, spec_changes=ctx.spec_changes,
                regenerated=inc.regenerated,
                preserved=[p + (" (stubs appended)" if p in inc.patched else "")
                           for p in inc.preserved],
            )
            st.detail = (f"incremental from {opts.previous_run}: {len(ctx.spec_changes)} spec "
                         f"change(s), regenerated {len(inc.regenerated)} file(s)")
        else:
            res = scaffold.scaffold(ctx.spec, spec_path, project, cfg.setup_timeout_s)
            if not res.models.ok:
                st.detail = res.models.output[-1000:]
                raise StopRun("model generation failed")
            st.detail = f"{len(res.files)} files generated"
    ctx.guard = Guardrails(cfg, project)

    _setup_station(ctx)

    # 3 + 4. Agents
    coder_status, coder_reason = None, ""
    if ctx.provider is None:
        skip(ctx, "test_writer", "--no-llm")
        with station(ctx, "baseline_checks") as st:
            st.checks = checks.run_all(project, cfg, ctx.run_dir / "checks" / "baseline")
            st.status = "passed" if all(c.passed for c in st.checks) else "failed"
            st.detail = " · ".join(f"{c.name}: {c.summary}" for c in st.checks)
        skip(ctx, "coder", "--no-llm")
    else:
        if diff is not None and diff.is_empty:
            skip(ctx, "test_writer", "no spec changes")
        else:
            with station(ctx, "test_writer") as st:
                st.status, st.detail = run_test_writer(ctx)
                if st.status != "passed":
                    raise StopRun("the test writer's suite was rejected by the quality gates")
        with station(ctx, "baseline_checks") as st:
            baseline = checks.run_all(project, cfg, ctx.run_dir / "checks" / "baseline")
            st.checks = baseline
            st.status = "passed" if all(c.passed for c in baseline) else "failed"
            st.detail = " · ".join(f"{c.name}: {c.summary}" for c in baseline)
        with station(ctx, "coder") as st:
            coder_status, coder_reason = run_coder(ctx, baseline)
            st.status = "passed" if coder_status == "passed" else "failed"
            st.detail = coder_reason

    # 5. Contract
    with station(ctx, "contract") as st:
        record.contract = contract.run_contract(project, cfg, ctx.run_dir)
        st.checks = [record.contract]
        st.status = "passed" if record.contract.passed else "failed"
        st.detail = record.contract.summary

    # 6. SDK
    with station(ctx, "sdk") as st:
        generated = sdk.generate_sdk(project, cfg.command_timeout_s)
        st.status = "passed" if generated.ok else "failed"
        st.detail = "client/ generated and importable" if generated.ok else generated.output[-500:]

    if prev_project is not None and record.incremental is not None:
        record.incremental.project_diff = incremental.project_diff(prev_project, project)

    # 7. Final status
    _final_status(ctx, coder_status, coder_reason)
