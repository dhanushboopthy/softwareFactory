"""Orchestrates the stations: lint → scaffold → setup → test writer → coder → contract → SDK.

The run record (and report) is saved after every station and every agent iteration.
"""

from __future__ import annotations

import hashlib
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from factory.config import FactoryConfig
from factory.harness import checks
from factory.harness.guardrails import Guardrails
from factory.harness.loop import Budget, BudgetExceeded
from factory.llm.base import LLMProvider
from factory.report.models import IncrementalInfo, RunRecord, StationResult, now
from factory.report.writer import load_record
from factory.run_context import RunContext
from factory.spec import load_raw, load_spec, parse_spec
from factory.stations import contract, incremental, lint, scaffold, sdk
from factory.stations.coder import run_coder
from factory.stations.test_writer import run_test_writer


@dataclass
class BuildOptions:
    spec_path: Path
    use_llm: bool = True
    previous_run: str | None = None
    run_id: str | None = None


class StopRun(Exception):
    """A station failed in a way that makes later stations pointless."""


EMPTY_SPEC = parse_spec({"paths": {}})  # placeholder until lint has accepted the real spec


def input_profile(raw: Mapping[str, Any]) -> str:
    """The rule profile an input document runs under (see config.PROFILES)."""
    if "openapi" in raw:
        return "openapi"
    if "entities" in raw:
        return "entities"
    raise ValueError("input has neither a top-level `openapi` nor a top-level `entities` key")


def _detect_profile(spec_path: Path) -> str:
    try:
        return input_profile(load_raw(spec_path))
    except (OSError, ValueError, yaml.YAMLError):
        return "openapi"  # unreadable or unrecognised input: lint reports the problem


def new_run_id(spec_path: Path) -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{spec_path.stem}"


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
    cfg = cfg.for_profile(_detect_profile(spec_path))
    run_id = opts.run_id or new_run_id(spec_path)
    run_dir = (cfg.runs_dir / run_id).resolve()
    if run_dir.exists():
        raise FileExistsError(f"run directory {run_dir} already exists")
    project = run_dir / "project"
    record = RunRecord(
        run_id=run_id,
        spec_name=spec_path.name,
        spec_path=str(spec_path),
        spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        provider=provider.name if provider and opts.use_llm else "none",
        model=provider.model if provider and opts.use_llm else "none",
        config=cfg.to_dict(),
    )
    ctx = RunContext(
        cfg=cfg, spec=EMPTY_SPEC, project=project,
        run_dir=run_dir, record=record, provider=provider if opts.use_llm else None,
        guard=Guardrails(cfg, run_dir), budget=Budget(cfg, record.usage), log=log,
    )
    log(f"run {run_id} → {run_dir}")
    try:
        _build(ctx, opts, spec_path)
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


def _build(ctx: RunContext, opts: BuildOptions, spec_path: Path) -> None:
    cfg, record, project = ctx.cfg, ctx.record, ctx.project
    ctx.run_dir.mkdir(parents=True)

    # 1. Lint
    with station(ctx, "lint") as st:
        if cfg.profile == "entities":
            st.detail = ("entities input is not supported yet (v2 core, PR 1 in "
                         "docs/designs/description-to-backend.md)")
            raise StopRun("entities input is not supported yet")
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

    # Project virtualenv (the generated project never shares the factory's environment)
    with station(ctx, "setup") as st:
        env = scaffold.setup_env(project, cfg.setup_timeout_s)
        st.detail = f"uv sync in {env.duration_s}s"
        if not env.ok:
            st.detail = env.output[-1000:]
            raise StopRun("could not create the project environment")

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
