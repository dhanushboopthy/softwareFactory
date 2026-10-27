"""Station 3: the test-writer agent writes behaviour tests before any implementation exists.

Its suite is accepted only if it collects, passes the static quality gate, fails against the
current (unimplemented) code, and fails against a deliberately hardcoded implementation.

Under the entities profile the writer covers only the LLM-written rules and actions (everything
else has generated tests), and the cheat probe is two fake rule sets instead (R4, R11, R13).
"""

from __future__ import annotations

from factory.harness import checks, test_quality
from factory.harness.context import build_test_writer_context
from factory.harness.guardrails import Role, matches, measure_change
from factory.harness.loop import run_turn
from factory.harness.tools import ToolExecutor
from factory.report.models import CheckResult, Iteration, StationStatus, now
from factory.run_context import RunContext

WRITER_TESTS = ("tests/test_*.py",)


def skip_reason(ctx: RunContext) -> str | None:
    """Why the station has nothing to do, or None. With no rule or action for the LLM to
    implement, the red check could never fail against no-op hooks."""
    if ctx.entities is not None and ctx.entities.llm_items() == 0:
        return ("entities.yaml has no rules or actions for the LLM; there is nothing to test "
                "beyond the generated tests")
    return None


def _writer_files(ctx: RunContext) -> dict[str, str]:
    return {
        rel: content for rel, content in ctx.guard.writable_files(Role.TEST_WRITER).items()
        if matches(rel, WRITER_TESTS)
    }


def _gates_v1(ctx: RunContext, log_dir_name: str) -> list[CheckResult]:
    log_dir = ctx.run_dir / "checks" / log_dir_name
    collected = checks.collect_tests(ctx.project, ctx.cfg, log_dir)
    if not collected.passed:
        return [collected]
    return [
        collected,
        test_quality.assess_tests(_writer_files(ctx), ctx.spec),
        test_quality.red_check(ctx.project, ctx.cfg, log_dir),
        test_quality.cheat_probe(ctx.project, ctx.spec, ctx.cfg, log_dir),
    ]


def _gates_v2(ctx: RunContext, log_dir_name: str) -> list[CheckResult]:
    doc, env = ctx.entities, ctx.check_env
    if doc is None or env is None:
        raise ValueError("the entities profile needs ctx.entities and ctx.check_env")
    log_dir = ctx.run_dir / "checks" / log_dir_name
    collected = checks.collect_tests(ctx.project, ctx.cfg, log_dir, env=env.fresh_env("collect"))
    if not collected.passed:
        return [collected]
    files = _writer_files(ctx)
    paths = sorted(files)
    gates = [
        collected,
        test_quality.assess_tests_v2(files, doc),
        test_quality.red_check(ctx.project, ctx.cfg, log_dir, test_paths=paths,
                               env=env.fresh_env("red")),
    ]
    if paths:  # without writer files there is nothing to run the probes on
        gates += test_quality.cheat_probes_v2(ctx.project, doc, ctx.cfg, log_dir, env, paths)
    return gates


def _gates(ctx: RunContext, log_dir_name: str) -> list[CheckResult]:
    if ctx.cfg.profile == "entities":
        return _gates_v2(ctx, log_dir_name)
    return _gates_v1(ctx, log_dir_name)


def _modules(ctx: RunContext) -> list[str]:
    """What the writer is told to cover: API tags (v1) or entities with LLM work (v2)."""
    doc = ctx.entities
    if doc is None:
        return ctx.spec.tags
    # A rule may be declared on one entity and target another's operations.
    targeted = {target.split(".", 1)[0] for _, rule in doc.all_rules() for target in rule.on}
    return [name for name in doc.ordered() if name in targeted or doc.entity(name).actions]


def run_test_writer(ctx: RunContext) -> tuple[StationStatus, str]:
    if ctx.provider is None:
        raise ValueError("the test-writer station needs an LLM provider")
    feedback = ""
    for attempt in range(1, ctx.cfg.test_writer_max_attempts + 1):
        ctx.log(f"  test_writer attempt {attempt}")
        it = Iteration(agent="test_writer", index=attempt)
        ctx.record.iterations.append(it)
        before = ctx.guard.writable_files(Role.TEST_WRITER)
        bundle = build_test_writer_context(
            project=ctx.project, cfg=ctx.cfg, guard=ctx.guard, attempt=attempt,
            feedback=feedback, modules=_modules(ctx), spec_changes=ctx.spec_changes,
            entities=ctx.entities,
        )
        ctx.record.prompt_versions.update(bundle.prompt_versions)
        record_violation = ctx.violation_recorder(it)
        executor = ToolExecutor(ctx.project, Role.TEST_WRITER, ctx.guard, record_violation,
                                attempt, ctx.cfg.tool_output_max_chars)
        try:
            turn = run_turn(provider=ctx.provider, system=bundle.system, user=bundle.user,
                            executor=executor, cfg=ctx.cfg, budget=ctx.budget,
                            agent="test_writer", record_violation=record_violation)
        finally:
            after = ctx.guard.writable_files(Role.TEST_WRITER)
            stats = measure_change(before, after)
            it.files_changed = stats.files
            it.lines_added, it.lines_removed = stats.lines_added, stats.lines_removed
            it.diff_path = ctx.write_diff(f"test_writer-{attempt:02d}", stats.diff)
            it.tool_calls = executor.calls
            it.ended_at = now()
            ctx.save()
        it.usage = turn.usage
        it.agent_message = turn.final_text[:2000]

        it.checks = _gates(ctx, f"test_writer-{attempt:02d}")
        it.all_passed = all(c.passed for c in it.checks)
        ctx.record.cheat_probes = [c for c in it.checks if c.name.startswith("cheat_")]
        for c in it.checks:
            ctx.log(f"    {c.name}: {'ok' if c.passed else 'FAIL'} — {c.summary}")
        if it.all_passed:
            ctx.save()
            return "passed", f"suite accepted on attempt {attempt}"
        feedback = checks.summarize_failures(it.checks, ctx.cfg.failure_summary_max_chars)
        it.failure_summary = feedback
        ctx.save()
    return "failed", f"suite rejected {ctx.cfg.test_writer_max_attempts} times"
