"""Station 4: the coder agent loop.

    for each iteration:
        context  = spec + AGENTS.md + current files + last failures   (rebuilt from disk)
        turn     = model ⇄ tools (read/list/write, guardrailed, ≤ N calls)
        reject   if the diff is too large (and revert it)
        results  = ruff, mypy, bandit, pytest, schemathesis           (run by the harness)
        stop     if everything passed, or the budget is exhausted
"""

from __future__ import annotations

from typing import Literal

from factory.harness import checks
from factory.harness.context import build_coder_context
from factory.harness.guardrails import Role, measure_change
from factory.harness.loop import BudgetExceeded, run_turn
from factory.harness.tools import ToolExecutor
from factory.report.models import CheckResult, Iteration, Violation, now
from factory.run_context import RunContext

CoderStatus = Literal["passed", "failed", "budget_exhausted"]


def _log_checks(ctx: RunContext, results: list[CheckResult]) -> None:
    ctx.log("    " + " · ".join(
        f"{r.name} {'ok' if r.passed else ('skip' if r.skipped else 'FAIL')}" for r in results))


def run_coder(ctx: RunContext, baseline: list[CheckResult]) -> tuple[CoderStatus, str]:
    if ctx.provider is None:
        raise ValueError("the coder station needs an LLM provider")
    cfg = ctx.cfg
    if all(r.passed for r in baseline):
        return "passed", "all checks already pass; nothing to implement"
    last_results = baseline
    last_errors = checks.summarize_failures(baseline, cfg.failure_summary_max_chars)

    for i in range(1, cfg.max_iters + 1):
        ctx.log(f"  coder iteration {i}/{cfg.max_iters}")
        it = Iteration(agent="coder", index=i)
        ctx.record.iterations.append(it)
        before = ctx.guard.writable_files(Role.CODER)
        bundle = build_coder_context(project=ctx.project, cfg=cfg, guard=ctx.guard, iteration=i,
                                     last_errors=last_errors, spec_changes=ctx.spec_changes)
        ctx.record.prompt_versions.update(bundle.prompt_versions)
        record_violation = ctx.violation_recorder(it)
        executor = ToolExecutor(ctx.project, Role.CODER, ctx.guard, record_violation, i,
                                cfg.tool_output_max_chars)
        try:
            turn = run_turn(provider=ctx.provider, system=bundle.system, user=bundle.user,
                            executor=executor, cfg=cfg, budget=ctx.budget, agent="coder",
                            record_violation=record_violation)
        except BudgetExceeded as exc:
            it.ended_at = now()
            ctx.save()
            return "budget_exhausted", str(exc)
        it.usage = turn.usage
        it.tool_calls = executor.calls
        it.agent_message = turn.final_text[:2000]

        stats = measure_change(before, ctx.guard.writable_files(Role.CODER))
        it.files_changed = stats.files
        it.lines_added, it.lines_removed = stats.lines_added, stats.lines_removed
        it.diff_path = ctx.write_diff(f"coder-{i:02d}", stats.diff)

        if stats.lines_changed > cfg.max_lines_changed_per_iter:
            executor.revert()
            reason = (f"changed {stats.lines_changed} lines (max "
                      f"{cfg.max_lines_changed_per_iter}); all changes reverted")
            record_violation(Violation(rule="diff_too_large", agent="coder", iteration=i,
                                       detail=reason))
            it.rejected, it.reject_reason = True, reason
            last_errors = (f"Iteration {i} was REJECTED: you {reason}. Make smaller, focused "
                           f"changes.\n\n{last_errors}")
            it.failure_summary = last_errors
            it.ended_at = now()
            ctx.save()
            continue

        if stats.files:
            results = checks.run_all(ctx.project, cfg, ctx.run_dir / "checks" / f"coder-{i:02d}")
        else:
            results = last_results  # nothing changed, so re-running checks would be wasted time
        _log_checks(ctx, results)
        it.checks = results
        it.all_passed = all(r.passed for r in results)
        it.ended_at = now()
        if it.all_passed:
            ctx.save()
            return "passed", f"all checks passed after {i} iteration(s)"
        last_results = results
        last_errors = checks.summarize_failures(results, cfg.failure_summary_max_chars)
        if not stats.files:
            last_errors = ("You made no file changes in the last iteration; the failures below "
                           f"are unchanged.\n\n{last_errors}")
        it.failure_summary = last_errors
        ctx.save()
        exhausted = ctx.budget.exceeded()
        if exhausted:
            return "budget_exhausted", exhausted
    return "failed", f"checks still failing after {cfg.max_iters} iterations"
