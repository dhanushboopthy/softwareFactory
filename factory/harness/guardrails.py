"""Guardrails: what each agent may read and write, and what code it may not write.

Every decision is returned as data (a Decision); callers record denials as Violations in the run
report instead of just raising. All limits come from FactoryConfig.
"""

from __future__ import annotations

import difflib
import functools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from factory.config import FactoryConfig


class Role(StrEnum):
    CODER = "coder"
    TEST_WRITER = "test_writer"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str | None = None
    detail: str = ""

    @classmethod
    def ok(cls) -> Decision:
        return cls(True)

    @classmethod
    def deny(cls, rule: str, detail: str) -> Decision:
        return cls(False, rule, detail)


@functools.lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob with `**` (any depth, including zero directories), `*` and `?` (never cross `/`)."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_regex(p).match(path) for p in patterns)


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    # `eval(` must not match `.evaluate(` or `session.exec(`-style method calls on objects.
    if pattern.endswith("(") and pattern[:-1].isidentifier():
        return re.compile(rf"(?<![\w.]){re.escape(pattern)}")
    return re.compile(re.escape(pattern))


class Guardrails:
    def __init__(self, cfg: FactoryConfig, project_root: Path) -> None:
        self.cfg = cfg
        self.root = project_root.resolve()
        self._forbidden = [(p, _pattern_regex(p)) for p in cfg.forbidden_patterns]

    def normalize(self, raw: str) -> str | None:
        """Project-relative POSIX path, or None if it escapes the project or is malformed."""
        if not raw or "\x00" in raw:
            return None
        pure = PurePosixPath(raw.strip().replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            return None
        rel = "" if str(pure) == "." else str(pure)  # PurePosixPath already drops "./" parts
        try:
            resolved = (self.root / rel).resolve()
            resolved.relative_to(self.root)
        except (ValueError, OSError):
            return None  # symlink pointing outside the project
        return rel

    def check_read(self, role: Role, path: str) -> Decision:
        if matches(path, self.cfg.unreadable):
            return Decision.deny("read_denied", f"{path} is not readable by any agent")
        if role is Role.TEST_WRITER and matches(path, self.cfg.test_writer_unreadable):
            return Decision.deny(
                "read_denied",
                f"{path} is implementation code; the test writer must not see the implementation",
            )
        return Decision.ok()

    def check_path_writable(self, role: Role, path: str) -> Decision:
        if matches(path, self.cfg.read_only) or (
            role is Role.CODER and matches(path, self.cfg.coder_read_only)
        ):
            return Decision.deny("read_only", f"{path} is read-only for {role}")
        allowed = self.cfg.coder_writable if role is Role.CODER else self.cfg.test_writer_writable
        if not matches(path, allowed):
            return Decision.deny(
                "path_not_allowed", f"{role} may only write {', '.join(allowed)}; got {path}"
            )
        if not path.endswith(self.cfg.writable_suffixes):
            return Decision.deny("path_not_allowed", f"only {self.cfg.writable_suffixes} files")
        return Decision.ok()

    def check_content(self, path: str, content: str) -> Decision:
        if len(content.encode()) > self.cfg.max_file_bytes:
            return Decision.deny(
                "file_too_large", f"{path} exceeds {self.cfg.max_file_bytes} bytes"
            )
        for pattern, regex in self._forbidden:
            m = regex.search(content)
            if m:
                line = content.count("\n", 0, m.start()) + 1
                return Decision.deny(
                    "forbidden_pattern", f"{path}:{line} contains forbidden pattern {pattern!r}"
                )
        ignores = content.count("# type: ignore")
        if ignores > self.cfg.max_type_ignores_per_file:
            return Decision.deny(
                "type_ignore_spam",
                f"{path} has {ignores} '# type: ignore' comments "
                f"(max {self.cfg.max_type_ignores_per_file}); fix the types instead",
            )
        return Decision.ok()

    def check_write(self, role: Role, path: str, content: str) -> Decision:
        decision = self.check_path_writable(role, path)
        if not decision.allowed:
            return decision
        return self.check_content(path, content)

    def writable_files(self, role: Role) -> dict[str, str]:
        """Current contents of every existing file the role may write (for snapshots/diffs)."""
        out: dict[str, str] = {}
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            if matches(rel, self.cfg.unreadable):
                continue
            if self.check_path_writable(role, rel).allowed:
                out[rel] = p.read_text(errors="replace")
        return out


@dataclass(frozen=True)
class ChangeStats:
    files: list[str]
    lines_added: int
    lines_removed: int
    diff: str

    @property
    def lines_changed(self) -> int:
        return self.lines_added + self.lines_removed


def measure_change(before: Mapping[str, str], after: Mapping[str, str]) -> ChangeStats:
    files, added, removed, chunks = [], 0, 0, []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        files.append(path)
        diff = list(difflib.unified_diff(
            (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
            fromfile=f"a/{path}" if old is not None else "/dev/null",
            tofile=f"b/{path}" if new is not None else "/dev/null",
        ))
        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        chunks.append("".join(diff))
    return ChangeStats(files=files, lines_added=added, lines_removed=removed,
                       diff="\n".join(chunks))
