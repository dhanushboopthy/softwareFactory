"""Phase 6: incremental builds. Reuse the previous run's project and change only what the spec
change requires.

Generated files that no agent has touched since generation (hash matches the manifest) are simply
regenerated. Agent-modified files are preserved; new operations are appended to them as stubs and
the agents are told exactly what changed.
"""

from __future__ import annotations

import ast
import difflib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from factory.report.models import FileChange
from factory.spec import ApiSpec, Operation, sha256_json
from factory.stations.scaffold import (
    MANIFEST,
    file_hash,
    generate_models,
    read_manifest,
    render_route_fn,
    render_service_stub,
    static_files,
)

CACHE_DIRS = (".hypothesis", ".schemathesis", ".ruff_cache", ".mypy_cache", ".pytest_cache",
              "__pycache__")
COPY_IGNORE = shutil.ignore_patterns(".venv", "client", "*.db", *CACHE_DIRS)
DIFF_EXCLUDE = (".venv", "client", ".factory", *CACHE_DIRS)


@dataclass
class SpecDiff:
    added: list[Operation] = field(default_factory=list)
    removed: list[Operation] = field(default_factory=list)
    changed: list[tuple[Operation, list[str]]] = field(default_factory=list)
    schema_changes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.schema_changes)

    def describe(self) -> list[str]:
        out = []
        for op in self.added:
            desc = " ".join(op.description.split())
            out.append(f"NEW operation {op.label} ({op.operation_id}): {op.summary}. {desc}"
                       .strip())
        for op in self.removed:
            out.append(f"REMOVED operation {op.label} ({op.operation_id}): remove its route and "
                       "service function")
        for op, what in self.changed:
            out.append(f"CHANGED operation {op.label} ({op.operation_id}): {', '.join(what)}")
        out.extend(self.schema_changes)
        return out


def _schema_change(name: str, old: dict[str, Any], new: dict[str, Any]) -> str:
    old_props, new_props = set(old.get("properties", {})), set(new.get("properties", {}))
    parts = []
    if added := sorted(new_props - old_props):
        parts.append(f"added properties {added}")
    if removed := sorted(old_props - new_props):
        parts.append(f"removed properties {removed}")
    changed = sorted(p for p in old_props & new_props
                     if old["properties"][p] != new["properties"][p])
    if changed:
        parts.append(f"changed properties {changed}")
    if set(old.get("required", [])) != set(new.get("required", [])):
        parts.append(f"required is now {sorted(new.get('required', []))}")
    if not parts:
        parts.append("definition changed")
    return f"SCHEMA {name}: " + "; ".join(parts)


def diff_specs(old: ApiSpec, new: ApiSpec) -> SpecDiff:
    diff = SpecDiff()
    old_ops = {o.operation_id: o for o in old.operations}
    new_ops = {o.operation_id: o for o in new.operations}
    diff.added = [new_ops[k] for k in new_ops if k not in old_ops]
    diff.removed = [old_ops[k] for k in old_ops if k not in new_ops]
    for key in new_ops.keys() & old_ops.keys():
        o, n = old_ops[key].signature(), new_ops[key].signature()
        if sha256_json(o) == sha256_json(n):
            continue
        what = [part for part in o if o[part] != n[part]]
        diff.changed.append((new_ops[key], what))
    diff.changed.sort(key=lambda c: c[0].operation_id)
    for name in sorted(new.schemas.keys() | old.schemas.keys()):
        if name not in old.schemas:
            diff.schema_changes.append(f"NEW schema {name}")
        elif name not in new.schemas:
            diff.schema_changes.append(f"REMOVED schema {name}")
        elif old.schemas[name] != new.schemas[name]:
            diff.schema_changes.append(_schema_change(name, old.schemas[name], new.schemas[name]))
    return diff


def _defined_functions(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


@dataclass
class IncrementalScaffold:
    regenerated: list[str]
    preserved: list[str]
    patched: list[str]


def incremental_scaffold(
    prev_project: Path, project: Path, spec: ApiSpec, spec_path: Path, diff: SpecDiff,
    timeout_s: float,
) -> IncrementalScaffold:
    project = project.resolve()
    shutil.copytree(prev_project, project, ignore=COPY_IGNORE)
    shutil.copyfile(spec_path, project / "openapi.yaml")
    manifest = read_manifest(project)
    regenerated, preserved, patched = [], [], []
    new_manifest = dict(manifest)

    models = generate_models(project / "openapi.yaml", project / "app" / "models.py", timeout_s,
                             spec)
    if not models.ok:
        raise RuntimeError(f"model generation failed:\n{models.output[-2000:]}")
    models_text = (project / "app/models.py").read_text()
    if file_hash(models_text) != manifest.get("app/models.py"):
        regenerated.append("app/models.py")
    new_manifest["app/models.py"] = file_hash(models_text)

    added_by_module: dict[str, list[Operation]] = {}
    for op in diff.added:
        added_by_module.setdefault(op.module, []).append(op)

    for rel, content in static_files(spec).items():
        dest = project / rel
        current = dest.read_text() if dest.exists() else None
        if current == content:
            continue
        untouched = current is not None and manifest.get(rel) == file_hash(current)
        if current is None or untouched:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            new_manifest[rel] = file_hash(content)
            regenerated.append(rel)
            continue
        # Agent-modified: keep it, but append stubs for operations it does not define yet.
        preserved.append(rel)
        module = Path(rel).stem
        new_ops = [op for op in added_by_module.get(module, [])
                   if op.func_name not in _defined_functions(current)]
        if rel.startswith("app/services/") and new_ops:
            stubs = "\n\n\n".join(render_service_stub(op) for op in new_ops)
            dest.write_text(current.rstrip() + "\n\n\n" + stubs + "\n")
            patched.append(rel)
        elif rel.startswith("app/routes/") and new_ops:
            fns = "\n\n\n".join(render_route_fn(op) for op in new_ops)
            dest.write_text(current.rstrip() + "\n\n\n" + fns + "\n")
            patched.append(rel)
    write_manifest_hashes(project, new_manifest)
    return IncrementalScaffold(regenerated=sorted(regenerated), preserved=sorted(preserved),
                               patched=sorted(patched))


def write_manifest_hashes(project: Path, hashes: dict[str, str]) -> None:
    path = project / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(hashes.items())), indent=2))


def _project_files(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or rel.parts[0] in DIFF_EXCLUDE or "__pycache__" in rel.parts:
            continue
        if p.suffix == ".db" or rel.as_posix() == "uv.lock":
            continue
        out[rel.as_posix()] = p.read_text(errors="replace")
    return out


def project_diff(prev_project: Path, project: Path) -> list[FileChange]:
    old, new = _project_files(prev_project), _project_files(project)
    changes = []
    for rel in sorted(old.keys() | new.keys()):
        a, b = old.get(rel), new.get(rel)
        if a == b:
            continue
        added = removed = 0
        for line in difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(),
                                         lineterm=""):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        kind: Literal["added", "removed", "modified"] = (
            "added" if a is None else "removed" if b is None else "modified"
        )
        changes.append(FileChange(path=rel, change=kind, lines_added=added, lines_removed=removed))
    return changes
