"""Read-only access to the files of a run, safe against traversal, symlinks and secrets.

Only a fixed set of places under a run directory can be viewed (the generated project, the
iteration diffs, the check and contract logs, the report), and never anything that could hold a
secret or is just noise (virtualenvs, caches, local environment files, databases).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from factory.export import EXCLUDED_DIRS, EXCLUDED_NAMES, EXCLUDED_SUFFIXES

VIEW_DIRS = ("project", "iterations", "checks", "contract", "deploy")
VIEW_FILES = ("report.md", "run.json", "error.log", "postgres.log")
MAX_VIEW_BYTES = 200_000
_HIDDEN_NAMES = EXCLUDED_DIRS | EXCLUDED_NAMES


class NotViewable(LookupError):
    """The path is not something the UI shows (missing, outside the allowed places, hidden)."""


@dataclass(frozen=True)
class Entry:
    name: str
    path: str  # relative to the run directory, "/"-separated
    is_dir: bool
    size: int


@dataclass(frozen=True)
class TextView:
    text: str
    truncated: bool
    binary: bool


def hidden(name: str) -> bool:
    return name in _HIDDEN_NAMES or name.endswith(EXCLUDED_SUFFIXES)


def resolve(run_dir: Path, rel: str) -> Path:
    """The file or directory ``rel`` under ``run_dir``; NotViewable unless it is allowed."""
    if rel in ("", "/"):
        return run_dir
    parts = rel.strip("/").split("/")
    if any(p in ("", ".", "..") or "\x00" in p or "\\" in p or hidden(p) for p in parts):
        raise NotViewable(rel)
    first = parts[0]
    if first not in VIEW_DIRS and not (first in VIEW_FILES and len(parts) == 1):
        raise NotViewable(rel)
    current = run_dir
    for part in parts:
        current = current / part
        if current.is_symlink():  # a link could point anywhere on this machine
            raise NotViewable(rel)
    if not current.exists():
        raise NotViewable(rel)
    root = run_dir.resolve()
    if root not in current.resolve().parents and current.resolve() != root:
        raise NotViewable(rel)
    return current


def list_dir(run_dir: Path, directory: Path) -> list[Entry]:
    """The viewable entries of ``directory`` (at the run root, only the allowed places)."""
    entries = []
    for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        if child.is_symlink() or hidden(child.name):
            continue
        if directory == run_dir and child.name not in (*VIEW_DIRS, *VIEW_FILES):
            continue
        entries.append(Entry(
            name=child.name, path=child.relative_to(run_dir).as_posix(), is_dir=child.is_dir(),
            size=0 if child.is_dir() else child.stat().st_size))
    return entries


def read_text(path: Path, limit: int = MAX_VIEW_BYTES) -> TextView:
    """The start of a text file; binary files are reported, not shown."""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    if b"\x00" in data:
        return TextView("", truncated=False, binary=True)
    return TextView(data.decode("utf-8", errors="replace"), truncated=truncated, binary=False)


def breadcrumbs(rel: str) -> list[tuple[str, str]]:
    """(label, path) for each folder on the way to ``rel``."""
    crumbs, so_far = [], ""
    for part in [p for p in rel.strip("/").split("/") if p]:
        so_far = f"{so_far}/{part}".strip("/")
        crumbs.append((part, so_far))
    return crumbs
