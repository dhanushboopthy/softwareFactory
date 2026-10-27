"""Drafts: the entities files being written, reviewed and approved in the UI.

A draft lives in ``drafts/<id>/``: ``entities.yaml`` (the file under review), ``draft.json`` (what
happened while drafting) and ``sources/`` (the documents it was drafted from). Saving a draft never
approves it; only the approve action writes ``approved: true``, and only for the exact text the
reviewer was shown (its hash travels with the approve form).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from factory.config import FactoryConfig
from factory.llm import get_provider
from factory.llm.base import LLMError, LLMProvider
from factory.report.runs import valid_run_id
from factory.stations import spec_writer
from factory.stations.spec_writer import DraftResult

DraftStatus = Literal["drafting", "ready", "failed"]
_APPROVED_LINE = re.compile(r"^approved[ \t]*:.*$", re.MULTILINE)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class DraftMeta(BaseModel):
    id: str
    created_at: datetime
    kind: Literal["generated", "uploaded"]
    sources: list[str] = Field(default_factory=list)  # names of the documents it came from
    status: DraftStatus = "drafting"
    message: str = ""  # why it failed, or a note for the reviewer
    attempts: int = 0
    cost_usd: float | None = None
    provider: str = ""
    model: str = ""
    errors: list[str] = Field(default_factory=list)  # problems of the last invalid answer
    gaps: list[str] = Field(default_factory=list)  # what the model said it could not express
    approved_at: datetime | None = None
    build_run_id: str | None = None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def safe_name(name: str, fallback: str = "document") -> str:
    """A file name made only of letters, digits and ._- (never a path)."""
    base = _UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip("._") or fallback
    return base[:80]


def set_approved(text: str, approved: bool) -> str:
    """``text`` with its top-level ``approved:`` line set to ``approved``; added if missing."""
    line = f"approved: {'true' if approved else 'false'}"
    if _APPROVED_LINE.search(text):
        return _APPROVED_LINE.sub(line, text, count=1)
    return f"{line}\n{text}"


def is_approved(text: str) -> bool:
    match = _APPROVED_LINE.search(text)
    return bool(match and match.group(0).split(":", 1)[1].strip().lower() == "true")


class DraftStore:
    def __init__(self, root: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
                 ) -> None:
        self.root, self.clock = root, clock

    def _dir(self, draft_id: str) -> Path:
        if not valid_run_id(draft_id):
            raise ValueError(f"{draft_id!r} is not a valid draft id")
        path = (self.root.resolve() / draft_id).resolve()
        if path.parent != self.root.resolve():
            raise ValueError(f"{draft_id!r} is outside the drafts directory")
        return path

    def new_id(self) -> str:
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        while True:
            candidate = f"d-{stamp}-{secrets.token_hex(2)}"
            if not (self.root / candidate).exists():
                return candidate

    def create(self, kind: Literal["generated", "uploaded"], sources: Mapping[str, str],
               status: DraftStatus = "drafting", entities_text: str | None = None) -> DraftMeta:
        draft_id = self.new_id()
        directory = self._dir(draft_id)
        (directory / "sources").mkdir(parents=True)
        names: list[str] = []
        for index, (name, text) in enumerate(sources.items(), start=1):
            stored = f"{index:02d}-{safe_name(name)}"
            (directory / "sources" / stored).write_text(text)
            names.append(stored)
        meta = DraftMeta(id=draft_id, created_at=self.clock(), kind=kind, sources=names,
                         status=status)
        if entities_text is not None:
            self.write_text(draft_id, entities_text)
        self._write_meta(meta)
        return meta

    def get(self, draft_id: str) -> DraftMeta | None:
        try:
            path = self._dir(draft_id) / "draft.json"
            if path.is_symlink() or not path.is_file():
                return None
            return DraftMeta.model_validate_json(path.read_text())
        except (ValueError, ValidationError, OSError):
            return None

    def update(self, draft_id: str, **changes: object) -> DraftMeta:
        meta = self.get(draft_id)
        if meta is None:
            raise KeyError(draft_id)
        updated = meta.model_copy(update=changes)
        self._write_meta(updated)
        return updated

    def all_drafts(self) -> list[DraftMeta]:
        if not self.root.is_dir():
            return []
        found = [m for child in self.root.iterdir() if child.is_dir() and not child.is_symlink()
                 and valid_run_id(child.name) and (m := self.get(child.name)) is not None]
        return sorted(found, key=lambda m: m.created_at, reverse=True)

    def entities_path(self, draft_id: str) -> Path:
        return self._dir(draft_id) / "entities.yaml"

    def source_paths(self, draft_id: str) -> list[Path]:
        meta = self.get(draft_id)
        return [self._dir(draft_id) / "sources" / n for n in (meta.sources if meta else [])]

    def has_text(self, draft_id: str) -> bool:
        return self.entities_path(draft_id).is_file()

    def read_text(self, draft_id: str) -> str:
        return self.entities_path(draft_id).read_text()

    def write_text(self, draft_id: str, text: str) -> None:
        _atomic_write(self.entities_path(draft_id), text)

    def _write_meta(self, meta: DraftMeta) -> None:
        _atomic_write(self._dir(meta.id) / "draft.json", meta.model_dump_json(indent=2))


def _atomic_write(path: Path, text: str) -> None:
    """Write so a reader never sees half a file (the page reloads while a draft is written)."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


DraftFn = Callable[..., DraftResult]
ProviderFn = Callable[[Mapping[str, str]], LLMProvider]


class DraftRunner:
    """Runs `factory draft` in a background thread, one at a time (it spends LLM money)."""

    def __init__(self, store: DraftStore, cfg: FactoryConfig, env: Mapping[str, str],
                 draft_fn: DraftFn = spec_writer.draft,
                 provider_fn: ProviderFn = get_provider) -> None:
        self.store, self.cfg, self.env = store, cfg, env
        self._draft, self._provider = draft_fn, provider_fn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, draft_id: str) -> threading.Thread | None:
        """Begin drafting ``draft_id`` in the background; None if another draft is running."""
        with self._lock:
            if self.busy():
                return None
            thread = threading.Thread(target=self._run, args=(draft_id,), daemon=True,
                                      name=f"draft-{draft_id}")
            self._thread = thread
            thread.start()
            return thread

    def _run(self, draft_id: str) -> None:
        try:
            provider = self._provider(self.env)
            result = self._draft(self.store.source_paths(draft_id),
                                 self.store.entities_path(draft_id), self.cfg, provider)
        except LLMError as exc:
            self._fail(draft_id, f"The model call failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - a thread must report, never die silently
            self._fail(draft_id, f"{type(exc).__name__}: {str(exc)[:300]}")
            return
        common = {"attempts": result.attempts, "cost_usd": result.usage.cost_usd,
                  "provider": provider.name, "model": provider.model}
        if result.path is not None:
            self.store.update(draft_id, status="ready", gaps=result.gaps, errors=[], **common)
            return
        # No valid answer: keep the last one so the reviewer can fix it by hand.
        if result.last_answer:
            meta = self.store.get(draft_id)
            self.store.write_text(draft_id, spec_writer.render_draft(
                result.last_answer, meta.sources if meta else []))
        self.store.update(draft_id, status="failed", errors=result.errors,
                          message="The model did not produce a valid file in "
                                  f"{result.attempts} attempts. Its last answer is below to "
                                  "fix by hand.", **common)

    def _fail(self, draft_id: str, message: str) -> None:
        self.store.update(draft_id, status="failed", message=message)
