"""`factory draft`: the spec writer turns a project description into an entities.yaml draft.

The model makes a single call per attempt and has no tools, so it cannot touch any file. The
harness validates its answer with the same schema the build uses and, if it is invalid, sends the
exact errors back (up to ``spec_writer_max_attempts``). Only a valid draft is ever written, and it
is written with ``approved: false`` as its first key: `factory build` refuses it until a human has
read it and changed that to ``true`` (decisions R1, R6, D18, D24).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, EntitiesError, expand, load_entities_text, parse_entities
from factory.harness.context import render_prompt
from factory.llm.base import LLMProvider, UserMessage
from factory.llm.pricing import estimate_cost
from factory.report.models import Usage
from factory.stations.lint import lint_spec


class DraftTooLarge(ValueError):
    """The documents do not fit the prompt; nothing is trimmed (decision D24)."""


@dataclass(frozen=True)
class Document:
    name: str
    text: str


@dataclass
class DraftResult:
    path: Path | None  # None when no valid draft was produced
    attempts: int
    errors: list[str]  # the problems of the last invalid answer (empty on success)
    usage: Usage
    gaps: list[str] = field(default_factory=list)
    prompt_versions: dict[str, str] = field(default_factory=dict)


def read_documents(paths: Sequence[Path]) -> list[Document]:
    docs = []
    for path in paths:
        try:
            docs.append(Document(path.name, path.read_text()))
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path} is not a UTF-8 text file") from exc
    return docs


def check_size(docs: Sequence[Document], limit: int) -> None:
    """Refuse before any model call if the documents exceed ``limit`` characters in total."""
    total = sum(len(d.text) for d in docs)
    if total > limit:
        lines = [f"  {d.name}: {len(d.text):,} characters" for d in docs]
        raise DraftTooLarge(
            f"the documents total {total:,} characters but the limit is {limit:,} "
            "(context_max_total_chars). Shorten or split them; nothing is ever trimmed:\n"
            + "\n".join(lines))


_FENCE = re.compile(r"```[\w-]*[ \t]*\n(.*?)```", re.DOTALL)  # any language tag, or none
_APPROVED = re.compile(r"^approved[ \t]*:.*\n?", re.MULTILINE)


def extract_yaml(text: str) -> str:
    """The YAML in the model's answer: the first fenced block that looks like an entities file,
    else the first fenced block, else the whole answer."""
    blocks = _FENCE.findall(text)
    chosen = next((b for b in blocks if re.search(r"^entities\s*:", b, re.MULTILINE)),
                  blocks[0] if blocks else text)
    return chosen.strip("\n") + "\n"


def strip_approved(yaml_text: str) -> str:
    """Remove any top-level `approved:` line: only the harness decides what that says."""
    return _APPROVED.sub("", yaml_text)


def render_draft(yaml_text: str, sources: Sequence[str]) -> str:
    """The file written to disk: a review banner, `approved: false` first, then the draft."""
    header = (
        f"# Drafted by API Factory from {', '.join(sources)}.\n"
        "# REVIEW EVERYTHING before approving: each entity and its owner, every rule and guard,\n"
        "# and the `gaps:` list at the bottom (what the description asked for but this format\n"
        "# cannot express).\n"
        "# When you agree with it, change the next line to `approved: true`;\n"
        "# `factory build` refuses the file until you do.\n"
    )
    return header + "approved: false\n" + strip_approved(yaml_text)


def validate_draft(yaml_text: str) -> tuple[EntitiesDoc | None, list[str]]:
    """(document, []) when the draft is a valid entities file whose expansion passes the OpenAPI
    lint; otherwise (None, problems), one line per problem."""
    try:
        raw = load_entities_text(strip_approved(yaml_text))
        if isinstance(raw, dict):
            raw["approved"] = False
        doc = parse_entities(raw)
    except EntitiesError as exc:
        return None, exc.problems
    lint = lint_spec(expand(doc))
    if not lint.ok:
        return None, [f"the OpenAPI expanded from this file failed lint: {e}" for e in lint.errors]
    return doc, []


def draft(
    docs: Sequence[Path], out: Path, cfg: FactoryConfig, provider: LLMProvider,
    log: Callable[[str], None] = lambda _: None,
) -> DraftResult:
    documents = read_documents(docs)
    check_size(documents, cfg.context_max_total_chars)
    system, system_hash = render_prompt("spec_writer.system.j2")
    versions = {"spec_writer.system.j2": system_hash}
    usage = Usage()
    previous = ""
    errors: list[str] = []
    for attempt in range(1, cfg.spec_writer_max_attempts + 1):
        user, user_hash = render_prompt(
            "spec_writer.user.j2", attempt=attempt, max_attempts=cfg.spec_writer_max_attempts,
            documents=documents, errors=errors, previous=previous)
        versions["spec_writer.user.j2"] = user_hash
        log(f"  spec_writer attempt {attempt}/{cfg.spec_writer_max_attempts}")
        response = provider.complete(system=system, messages=[UserMessage(user)], tools=(),
                                     agent="spec_writer", max_tokens=cfg.llm_max_output_tokens)
        response.usage.cost_usd = estimate_cost(provider.model, response.usage)
        usage.add(response.usage)
        if response.stop_reason in ("max_tokens", "refusal"):
            errors = [f"the model stopped early ({response.stop_reason}); answer with the "
                      "complete file only"]
            previous = response.text
            log(f"    {errors[0]}")
            continue
        previous = extract_yaml(response.text)
        doc, errors = validate_draft(previous)
        if doc is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_draft(previous, [d.name for d in documents]))
            return DraftResult(out, attempt, [], usage, list(doc.gaps), versions)
        log(f"    invalid: {len(errors)} problem(s)")
    return DraftResult(None, cfg.spec_writer_max_attempts, errors, usage, [], versions)
