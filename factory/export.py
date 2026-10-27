"""Zip archives of a run's generated project, for download.

Only what is worth handing to someone: the source, migrations, tests, spec, Docker files and
client SDK. Virtualenvs, caches, databases, local environment files and symlinks are left out (a
symlink could point anywhere on this machine).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

EXCLUDED_DIRS = frozenset({
    ".venv", "__pycache__", ".hypothesis", ".schemathesis", ".ruff_cache", ".mypy_cache",
    ".pytest_cache", ".factory", ".git",
})
EXCLUDED_NAMES = frozenset({".env"})
EXCLUDED_SUFFIXES = (".db", ".pyc", ".sqlite", ".sqlite3")
# A fixed timestamp makes the archive depend only on the files' contents.
_EPOCH = (2026, 1, 1, 0, 0, 0)


def _wanted(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    relative = path.relative_to(root)
    if any(part in EXCLUDED_DIRS for part in relative.parts[:-1]):
        return False
    return path.name not in EXCLUDED_NAMES and not path.name.endswith(EXCLUDED_SUFFIXES)


def _zip(root: Path, prefix: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if _wanted(root, path):
                info = zipfile.ZipInfo(f"{prefix}/{path.relative_to(root).as_posix()}", _EPOCH)
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def project_archive(project: Path, name: str) -> bytes:
    """The generated project (including ``client/``) as a zip whose top folder is ``name``."""
    if not project.is_dir():
        raise FileNotFoundError(f"{project} does not exist")
    return _zip(project, name)


def client_archive(project: Path, name: str) -> bytes:
    """Only the generated client SDK (``client/``)."""
    client = project / "client"
    if not client.is_dir() or client.is_symlink():
        raise FileNotFoundError("this run has no generated client")
    return _zip(client, name)
