"""Build the prompt context for each agent call.

Context is rebuilt from disk at every coder iteration (not accumulated chat history), so each
iteration sees the current files plus a bounded summary of the last failures. Prompt text lives in
versioned Jinja2 templates under factory/templates/prompts/; their hashes go into the run report.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from factory.config import FactoryConfig
from factory.harness.guardrails import Guardrails, Role, matches

_loader = PackageLoader("factory", "templates/prompts")
_env = Environment(
    loader=_loader,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # noqa: S701 - prompts are plain text, never HTML
)


@dataclass
class ContextBundle:
    system: str
    user: str
    prompt_versions: dict[str, str] = field(default_factory=dict)


def template_hash(name: str) -> str:
    source, _, _ = _loader.get_source(_env, name)
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def _render(name: str, **ctx: object) -> tuple[str, str]:
    return _env.get_template(name).render(**ctx).strip() + "\n", template_hash(name)


def file_tree(project: Path, cfg: FactoryConfig) -> str:
    lines = []
    for p in sorted(project.rglob("*")):
        rel = p.relative_to(project).as_posix()
        if p.is_file() and not matches(rel, (*cfg.unreadable, "client/**", ".factory/**")):
            lines.append(rel)
    return "\n".join(lines)


def gather_files(
    project: Path, patterns: Sequence[str], cfg: FactoryConfig, guard: Guardrails, role: Role,
) -> dict[str, str]:
    """Readable files matching ``patterns``, each truncated, total capped (first match wins)."""
    out: dict[str, str] = {}
    total = 0
    for p in sorted(project.rglob("*")):
        rel = p.relative_to(project).as_posix()
        if not p.is_file() or not matches(rel, patterns):
            continue
        if not guard.check_read(role, rel).allowed:
            continue
        text = p.read_text(errors="replace")
        if len(text) > cfg.context_max_file_chars:
            text = text[: cfg.context_max_file_chars] + "\n...(truncated; use read_file)"
        if total + len(text) > cfg.context_max_total_chars:
            out[rel] = "(omitted to stay within the context budget; use read_file)"
            continue
        out[rel] = text
        total += len(text)
    return out


def _read(project: Path, rel: str) -> str:
    path = project / rel
    return path.read_text() if path.exists() else ""


CODER_FILES = ("app/**/*.py", "tests/*.py")
TEST_WRITER_FILES = ("app/models.py", "tests/*.py")


def build_coder_context(
    *,
    project: Path,
    cfg: FactoryConfig,
    guard: Guardrails,
    iteration: int,
    last_errors: str,
    spec_changes: Iterable[str] = (),
) -> ContextBundle:
    system, h_sys = _render("coder.system.j2", writable=cfg.coder_writable,
                            max_tool_calls=cfg.max_tool_calls_per_iter,
                            max_lines=cfg.max_lines_changed_per_iter)
    user, h_user = _render(
        "coder.user.j2",
        iteration=iteration,
        max_iters=cfg.max_iters,
        agents_md=_read(project, "AGENTS.md"),
        spec=_read(project, "openapi.yaml"),
        tree=file_tree(project, cfg),
        files=gather_files(project, CODER_FILES, cfg, guard, Role.CODER),
        last_errors=last_errors,
        spec_changes=list(spec_changes),
    )
    return ContextBundle(system, user, {"coder.system.j2": h_sys, "coder.user.j2": h_user})


def build_test_writer_context(
    *,
    project: Path,
    cfg: FactoryConfig,
    guard: Guardrails,
    attempt: int,
    feedback: str,
    modules: Sequence[str],
    spec_changes: Iterable[str] = (),
) -> ContextBundle:
    system, h_sys = _render("test_writer.system.j2", writable=cfg.test_writer_writable,
                            max_tool_calls=cfg.max_tool_calls_per_iter)
    user, h_user = _render(
        "test_writer.user.j2",
        attempt=attempt,
        max_attempts=cfg.test_writer_max_attempts,
        agents_md=_read(project, "AGENTS.md"),
        spec=_read(project, "openapi.yaml"),
        files=gather_files(project, TEST_WRITER_FILES, cfg, guard, Role.TEST_WRITER),
        modules=list(modules),
        feedback=feedback,
        spec_changes=list(spec_changes),
    )
    return ContextBundle(system, user,
                         {"test_writer.system.j2": h_sys, "test_writer.user.j2": h_user})
