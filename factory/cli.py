"""`factory` CLI entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from factory import __version__
from factory.config import FactoryConfig

app = typer.Typer(
    name="factory",
    help="Spec-to-backend software factory: OpenAPI spec in, verified FastAPI backend out.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"factory {__version__}")
        raise typer.Exit()


def _log(msg: str) -> None:
    typer.echo(msg, err=True)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """Spec-to-backend software factory."""
    load_dotenv(override=False)


@app.command()
def build(
    spec: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, readable=True, help="OpenAPI 3.x spec (YAML/JSON)."
        ),
    ],
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Deterministic stations only (no agents).")
    ] = False,
    from_run: Annotated[
        str | None,
        typer.Option("--from", help="Incremental build on top of this previous run id."),
    ] = None,
    incremental: Annotated[
        bool,
        typer.Option("--incremental", help="Incremental build on the latest passed run of SPEC."),
    ] = False,
    run_id: Annotated[
        str | None, typer.Option(help="Explicit run id (default: timestamp).")
    ] = None,
) -> None:
    """Lint, scaffold, test, implement, verify and package a backend from SPEC.

    SPEC is an OpenAPI 3.x file, or an approved entities file (`approved: true`).
    """
    from factory.llm import ProviderConfigError, get_provider
    from factory.pipeline import (
        INCREMENTAL_REFUSAL,
        BuildOptions,
        IncrementalNotSupported,
        detect_profile,
        latest_passed_run,
    )
    from factory.pipeline import build as run_build

    cfg = FactoryConfig.from_env()
    if detect_profile(spec) == "entities" and (from_run or incremental):
        _log(INCREMENTAL_REFUSAL)
        raise typer.Exit(code=2)
    previous = from_run
    if incremental and previous is None:
        previous = latest_passed_run(cfg, spec.name)
        if previous is None:
            _log(f"no passed run of {spec.name} to build on; doing a full build")
    provider = None
    if not no_llm:
        try:
            provider = get_provider()
        except ProviderConfigError as exc:
            _log(f"LLM provider not configured: {exc}\n"
                 "Set LLM_PROVIDER in .env, or pass --no-llm for the deterministic pipeline.")
            raise typer.Exit(code=2) from exc
    try:
        record = run_build(
            BuildOptions(spec_path=spec, use_llm=not no_llm, previous_run=previous,
                         run_id=run_id),
            cfg, provider, log=_log,
        )
    except IncrementalNotSupported as exc:  # also guards callers that skip the check above
        _log(str(exc))
        raise typer.Exit(code=2) from exc
    typer.echo(str(cfg.runs_dir / record.run_id / "report.md"))
    raise typer.Exit(code=0 if record.status == "passed" else 1)


@app.command()
def draft(
    docs: Annotated[
        list[Path],
        typer.Argument(exists=True, dir_okay=False, readable=True,
                       help="The project description and design documents (text files)."),
    ],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the draft entities.yaml.")
    ],
    force: Annotated[bool, typer.Option("--force", help="Overwrite OUTPUT if it exists.")] = False,
) -> None:
    """Draft an entities.yaml from documents, for a human to review and approve.

    The draft is written with `approved: false`; `factory build` refuses it until you read it and
    change that to `approved: true`.
    """
    from factory.llm import ProviderConfigError, get_provider
    from factory.llm.base import LLMError
    from factory.stations.spec_writer import DraftTooLarge
    from factory.stations.spec_writer import draft as run_draft

    if output.exists() and not force:
        _log(f"{output} already exists; pass --force to overwrite it")
        raise typer.Exit(code=2)
    cfg = FactoryConfig.from_env()
    try:
        provider = get_provider()
    except ProviderConfigError as exc:
        _log(f"LLM provider not configured: {exc}\nSet LLM_PROVIDER in .env.")
        raise typer.Exit(code=2) from exc
    try:
        result = run_draft(docs, output, cfg, provider, log=_log)
    except (DraftTooLarge, ValueError) as exc:  # input problems; no model call was made
        _log(str(exc))
        raise typer.Exit(code=2) from exc
    except LLMError as exc:
        _log(f"LLM call failed: {exc}")
        raise typer.Exit(code=1) from exc
    cost = "n/a" if result.usage.cost_usd is None else f"${result.usage.cost_usd:.4f}"
    _log(f"{result.attempts} attempt(s), {result.usage.input_tokens} in / "
         f"{result.usage.output_tokens} out tokens, {cost}")
    if result.path is None:
        _log("no valid entities file after all attempts; the last problems were:")
        for problem in result.errors:
            _log(f"  - {problem}")
        raise typer.Exit(code=1)
    typer.echo(str(result.path))
    if result.gaps:
        _log("The description asks for things the format cannot express (see `gaps:`):")
        for gap in result.gaps:
            _log(f"  - {gap}")
    _log("Review the file, then set `approved: true` and run `factory build`.")


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run id (folder under runs/), or 'latest'.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print run.json instead.")] = False,
) -> None:
    """Print the report of a previous run."""
    cfg = FactoryConfig.from_env()
    if run_id == "latest":
        from factory.pipeline import list_runs

        runs = list_runs(cfg)
        if not runs:
            _log("no runs yet")
            raise typer.Exit(code=1)
        run_id = runs[-1].run_id
    run_dir = cfg.runs_dir / run_id
    target = run_dir / ("run.json" if as_json else "report.md")
    if not target.exists():
        _log(f"no report at {target}")
        raise typer.Exit(code=1)
    typer.echo(target.read_text())


@app.command()
def runs() -> None:
    """List previous runs."""
    from factory.pipeline import list_runs

    for r in list_runs(FactoryConfig.from_env()):
        cost = "n/a" if r.usage.cost_usd is None else f"${r.usage.cost_usd:.4f}"
        typer.echo(f"{r.run_id:40} {r.status:17} {r.model:22} iters={len(r.iterations):<3} {cost}")


@app.command("llm-ping")
def llm_ping() -> None:
    """Dry-run: one LLM call with the factory's tools attached; nothing is executed."""
    from factory.harness.tools import TOOLS
    from factory.llm import ProviderConfigError, get_provider
    from factory.llm.base import LLMError, UserMessage
    from factory.llm.pricing import estimate_cost

    try:
        provider = get_provider()
    except ProviderConfigError as exc:
        _log(str(exc))
        raise typer.Exit(code=2) from exc
    try:
        response = provider.complete(
            system="You are a connectivity check for a tool-using agent harness.",
            messages=[UserMessage("Call list_files once with directory '' and then stop.")],
            tools=TOOLS, agent="ping", max_tokens=1024,
        )
    except LLMError as exc:
        _log(f"LLM call failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({
        "provider": provider.name,
        "model": response.model,
        "stop_reason": response.stop_reason,
        "text": response.text,
        "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in response.tool_calls],
        "usage": response.usage.model_dump(),
        "cost_usd": estimate_cost(provider.model, response.usage),
    }, indent=2))
