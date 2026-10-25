"""The only tools an agent gets: read_file, list_files, write_file.

`run_checks` is deliberately not a tool: the harness runs verification itself after the agent's
turn, so the agent cannot skip, fake or reinterpret it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory.harness.guardrails import Guardrails, Role, matches
from factory.llm.base import ToolCall, ToolResult, ToolSpec
from factory.report.models import Violation

READ_FILE = ToolSpec(
    name="read_file",
    description="Read a UTF-8 text file from the project. Paths are relative to the project root.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "e.g. app/services/orders.py"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)
LIST_FILES = ToolSpec(
    name="list_files",
    description="List files under a project directory (recursive). Use '' for the project root.",
    input_schema={
        "type": "object",
        "properties": {"directory": {"type": "string", "description": "e.g. app/services"}},
        "required": ["directory"],
        "additionalProperties": False,
    },
)
WRITE_FILE = ToolSpec(
    name="write_file",
    description=(
        "Create or overwrite a file with the complete new content (not a patch). Only paths your "
        "role may write are accepted; rejected writes are reported back to you."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "e.g. app/services/orders.py"},
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)
TOOLS = (READ_FILE, LIST_FILES, WRITE_FILE)
MAX_LIST_ENTRIES = 500


class ToolExecutor:
    """Executes tool calls for one agent iteration, enforcing guardrails and tracking writes."""

    def __init__(
        self,
        project: Path,
        role: Role,
        guard: Guardrails,
        record_violation: Callable[[Violation], None],
        iteration: int,
        max_output_chars: int,
    ) -> None:
        self.project = project.resolve()
        self.role = role
        self.guard = guard
        self.record_violation = record_violation
        self.iteration = iteration
        self.max_output_chars = max_output_chars
        self.originals: dict[str, str | None] = {}  # first-touch content, for revert
        self.calls = 0

    def _violation(self, rule: str, path: str | None, detail: str) -> None:
        self.record_violation(Violation(rule=rule, agent=str(self.role), path=path, detail=detail,
                                        iteration=self.iteration))

    def _error(self, call: ToolCall, msg: str) -> ToolResult:
        return ToolResult(call_id=call.id, content=msg, is_error=True)

    def _str_arg(self, args: dict[str, Any], key: str) -> str | None:
        value = args.get(key)
        return value if isinstance(value, str) else None

    def execute(self, call: ToolCall) -> ToolResult:
        self.calls += 1
        if "__invalid__" in call.arguments:
            return self._error(call, "Tool arguments were not valid JSON; resend the call.")
        handler = {"read_file": self._read, "list_files": self._list,
                   "write_file": self._write}.get(call.name)
        if handler is None:
            return self._error(call, f"Unknown tool {call.name!r}. Available: read_file, "
                                     "list_files, write_file.")
        return handler(call)

    def _resolve(self, call: ToolCall, key: str) -> str | ToolResult:
        raw = self._str_arg(call.arguments, key)
        if raw is None:
            return self._error(call, f"Missing string argument {key!r}.")
        path = self.guard.normalize(raw)
        if path is None:
            self._violation("invalid_path", raw, f"path {raw!r} escapes the project or is invalid")
            return self._error(call, f"Invalid path {raw!r}: use a relative project path.")
        return path

    def _read(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call, "path")
        if isinstance(path, ToolResult):
            return path
        decision = self.guard.check_read(self.role, path)
        if not decision.allowed:
            self._violation(decision.rule or "read_denied", path, decision.detail)
            return self._error(call, f"BLOCKED ({decision.rule}): {decision.detail}")
        file = self.project / path
        if not file.is_file():
            return self._error(call, f"{path} does not exist.")
        text = file.read_text(errors="replace")
        if len(text) > self.max_output_chars:
            text = text[: self.max_output_chars] + "\n...(truncated)"
        return ToolResult(call_id=call.id, content=text)

    def _list(self, call: ToolCall) -> ToolResult:
        raw = self._str_arg(call.arguments, "directory") or ""
        base = self.guard.normalize(raw) if raw else ""
        if base is None:
            self._violation("invalid_path", raw, f"path {raw!r} escapes the project or is invalid")
            return self._error(call, f"Invalid directory {raw!r}.")
        root = self.project / base
        if not root.is_dir():
            return self._error(call, f"{base or '.'} is not a directory.")
        entries = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(self.project).as_posix()
            if p.is_file() and not matches(rel, self.guard.cfg.unreadable):
                entries.append(rel)
        if len(entries) > MAX_LIST_ENTRIES:
            entries = entries[:MAX_LIST_ENTRIES] + ["...(truncated)"]
        return ToolResult(call_id=call.id, content="\n".join(entries) or "(empty)")

    def _write(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call, "path")
        if isinstance(path, ToolResult):
            return path
        content = self._str_arg(call.arguments, "content")
        if content is None:
            return self._error(call, "Missing string argument 'content'.")
        decision = self.guard.check_write(self.role, path, content)
        if not decision.allowed:
            self._violation(decision.rule or "write_denied", path, decision.detail)
            return self._error(call, f"BLOCKED ({decision.rule}): {decision.detail}. "
                                     "The file was not written.")
        file = self.project / path
        if path not in self.originals:
            self.originals[path] = file.read_text() if file.exists() else None
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return ToolResult(call_id=call.id,
                          content=f"Wrote {path} ({content.count(chr(10)) + 1} lines).")

    def revert(self) -> None:
        """Undo every write made through this executor."""
        for path, original in self.originals.items():
            file = self.project / path
            if original is None:
                file.unlink(missing_ok=True)
            else:
                file.write_text(original)
        self.originals.clear()


def describe_call(call: ToolCall) -> str:
    args = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in call.arguments.items()}
    return f"{call.name}({json.dumps(args)})"
