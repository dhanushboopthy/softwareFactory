"""Factory configuration: budgets, limits and allowlists.

All guardrail knobs live here so a run's behaviour is fully described by one object, which the
run report serialises alongside the results. Scalar limits can be overridden via ``FACTORY_*``
env vars; allowlists and forbidden patterns cannot (they are safety rules, not tuning knobs).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
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


@dataclass(frozen=True)
class FactoryConfig:
    # Agent loop
    max_iters: int = 8
    max_tool_calls_per_iter: int = 20
    max_lines_changed_per_iter: int = 400
    test_writer_max_attempts: int = 3
    llm_max_output_tokens: int = 32_000

    # Sandbox
    command_timeout_s: int = 120
    setup_timeout_s: int = 600
    server_start_timeout_s: int = 30

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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FactoryConfig:
        """Build a config from defaults, overriding scalar fields from ``FACTORY_<FIELD>`` vars."""
        env = os.environ if env is None else env
        overrides: dict[str, Any] = {}
        for f in fields(cls):
            raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None:
                continue
            # With `from __future__ import annotations`, f.type is the annotation string.
            if f.type == "int":
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
