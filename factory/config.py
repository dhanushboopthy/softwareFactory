"""Factory configuration: budgets, limits and allowlists.

All guardrail knobs live here so a run's behaviour is fully described by one object, which the
run report serialises alongside the results. Scalar limits can be overridden via ``FACTORY_*``
env vars; allowlists and forbidden patterns cannot (they are safety rules, not tuning knobs).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

ENV_PREFIX = "FACTORY_"

# USD per million tokens (input, output). Source: Anthropic pricing as of 2026-10-06.
# Models not listed here get cost_usd=None unless LLM_PRICE_IN/LLM_PRICE_OUT are set; the token
# budget still applies.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "scripted": (0.0, 0.0),
}

DEFAULT_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "eval(",
    "exec(",
    "subprocess",
    "os.system",
    "pytest.skip",
    "pytest.mark.skip",
    "pytest.xfail",
    "pytest.mark.xfail",
    "importlib",
    "__import__",
)

# Rule profiles. The input document picks one (see pipeline.input_profile): an OpenAPI spec runs
# the "openapi" profile, which is exactly the FactoryConfig defaults below; an entities file runs
# "entities", where CRUD, ownership, ORM, migrations, routes and wiring are generated read-only and
# the coder writes only rule hooks (docs/designs/description-to-backend.md, R2-R4, R7).
PROFILES: tuple[str, ...] = ("openapi", "entities")

# Under the entities profile only app/rules/** is coder-writable; this lists what else exists and
# is generated, so the report and the guardrail messages say "read-only", not just "not allowed".
ENTITIES_READ_ONLY: tuple[str, ...] = (
    "entities.yaml",
    "tests/generated/**",
    "app/routes/**",
    "app/crud/**",
    "app/main.py",
    "app/orm.py",
    "app/auth.py",
    "app/settings.py",
    "app/db.py",
    "app/errors.py",
    "migrations/**",
    "alembic.ini",
    "Dockerfile",
    "compose.yaml",
    ".dockerignore",
)

# DDL strings agents may not write under the entities profile. A tripwire, not the enforcement:
# the schema comes from generated read-only ORM and migrations, applied with `alembic upgrade head`.
ENTITIES_DDL_PATTERNS: tuple[str, ...] = (
    "create_table",
    "ALTER TABLE",
    "metadata.create_all",
    "alembic",
)


@dataclass(frozen=True)
class FactoryConfig:
    # Agent loop
    max_iters: int = 8
    max_tool_calls_per_iter: int = 20
    max_lines_changed_per_iter: int = 400
    test_writer_max_attempts: int = 3
    spec_writer_max_attempts: int = 3  # `factory draft`: tries to produce a valid entities file
    llm_max_output_tokens: int = 32_000

    # Sandbox
    command_timeout_s: int = 120
    setup_timeout_s: int = 600
    server_start_timeout_s: int = 30

    # Entities-profile budgets scale with the work the LLM does (rules + actions), R18:
    # limit = min(cap, base + per_item * items). Used by scaled_for(); the OpenAPI profile keeps
    # the fixed limits above.
    budget_iters_base: int = 4
    budget_iters_per_item: int = 2
    budget_iters_cap: int = 16
    budget_lines_base: int = 150
    budget_lines_per_item: int = 100
    budget_lines_cap: int = 600
    budget_cost_base: float = 1.0
    budget_cost_per_item: float = 0.75
    budget_cost_cap: float = 10.0

    # Generated list endpoints (entities profile): `limit` default and maximum (R10, decision D12).
    list_limit_default: int = 50
    list_limit_max: int = 100

    # Postgres for entities-profile checks (harness/postgres.py). "docker" runs one container per
    # run; "local" starts a server from the binaries in postgres_bin_dir (for hosts without Docker).
    postgres_backend: str = "docker"
    postgres_image: str = "postgres:16-alpine"
    postgres_bin_dir: str = "/usr/lib/postgresql/16/bin"
    postgres_ready_timeout_s: int = 60

    # Deploy check (entities profile): `docker compose up` of the generated project, then /health
    # and one authenticated round trip. Needs Docker, so it is skipped when disabled (for example
    # FACTORY_DEPLOY_CHECK=0 on a machine that can only run the local Postgres backend).
    deploy_check: bool = True
    deploy_timeout_s: int = 600

    # Run budget (loop stops with status "budget_exhausted" when either is exceeded)
    max_tokens_per_run: int = 2_000_000
    max_cost_usd_per_run: float = 5.0

    # Context building (characters, not tokens; cheap and deterministic)
    context_max_file_chars: int = 30_000
    context_max_total_chars: int = 200_000
    failure_summary_max_chars: int = 6_000
    tool_output_max_chars: int = 40_000

    # Contract testing
    schemathesis_max_examples: int = 30
    schemathesis_seed: int = 42
    schemathesis_checks: str = "all"

    # Paths are relative to the generated project root (runs/<run_id>/project/)
    coder_writable: tuple[str, ...] = (
        "app/routes/**",
        "app/services/**",
        "app/db.py",
        "app/main.py",
    )
    test_writer_writable: tuple[str, ...] = ("tests/test_*.py",)
    # Read-only for every agent, regardless of the writable lists above.
    read_only: tuple[str, ...] = (
        "openapi.yaml",
        "AGENTS.md",
        "pyproject.toml",
        "uv.lock",
        "app/models.py",
        "tests/conftest.py",
        "client/**",
    )
    # Read-only for the coder specifically (the test writer owns tests/test_*.py).
    coder_read_only: tuple[str, ...] = ("tests/**",)
    # The test writer must never see the implementation.
    test_writer_unreadable: tuple[str, ...] = ("app/routes/**", "app/services/**", "app/db.py")
    # Nobody reads these (noise, binaries, or secrets-adjacent).
    unreadable: tuple[str, ...] = (
        ".venv/**", "**/__pycache__/**", "*.db", ".env*", ".hypothesis/**", ".schemathesis/**",
        ".ruff_cache/**", ".mypy_cache/**", ".pytest_cache/**",
    )
    writable_suffixes: tuple[str, ...] = (".py",)

    forbidden_patterns: tuple[str, ...] = DEFAULT_FORBIDDEN_PATTERNS
    # More than this many "# type: ignore" comments in one file is treated as spam.
    max_type_ignores_per_file: int = 2
    max_file_bytes: int = 200_000

    runs_dir: Path = field(default_factory=lambda: Path("runs"))

    # Chosen from the input document, never from the environment.
    profile: str = "openapi"

    def for_profile(self, profile: str) -> FactoryConfig:
        """This config with the allowlists of ``profile``; budgets and limits are unchanged."""
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
        if profile == "openapi":
            return replace(self, profile=profile)
        return replace(
            self,
            profile=profile,
            coder_writable=("app/rules/**",),
            read_only=(*self.read_only, *ENTITIES_READ_ONLY),
            test_writer_unreadable=(*self.test_writer_unreadable, "app/rules/**"),
            forbidden_patterns=(*self.forbidden_patterns, *ENTITIES_DDL_PATTERNS),
        )

    def scaled_for(self, llm_items: int) -> FactoryConfig:
        """This config with iteration, line and cost limits sized for ``llm_items`` rules and
        actions (base + per item, capped). Only the entities profile calls this."""
        if llm_items < 0:
            raise ValueError("llm_items must not be negative")
        return replace(
            self,
            max_iters=min(self.budget_iters_cap,
                          self.budget_iters_base + self.budget_iters_per_item * llm_items),
            max_lines_changed_per_iter=min(
                self.budget_lines_cap,
                self.budget_lines_base + self.budget_lines_per_item * llm_items),
            max_cost_usd_per_run=min(
                self.budget_cost_cap,
                round(self.budget_cost_base + self.budget_cost_per_item * llm_items, 6)),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FactoryConfig:
        """Build a config from defaults, overriding scalar fields from ``FACTORY_<FIELD>`` vars."""
        env = os.environ if env is None else env
        overrides: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "profile":
                continue
            raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None:
                continue
            # With `from __future__ import annotations`, f.type is the annotation string.
            if f.type == "bool":
                overrides[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif f.type == "int":
                overrides[f.name] = int(raw)
            elif f.type == "float":
                overrides[f.name] = float(raw)
            elif f.type == "str":
                overrides[f.name] = raw
            elif f.type == "Path":
                overrides[f.name] = Path(raw)
        return cls(**overrides)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form for the run report."""
        data = asdict(self)
        data["runs_dir"] = str(self.runs_dir)
        return data
