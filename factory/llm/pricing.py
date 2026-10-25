"""Cost estimation from token usage."""

from __future__ import annotations

import os
from collections.abc import Mapping

from factory.config import MODEL_PRICES_PER_MTOK
from factory.report.models import Usage

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def prices_for(model: str, env: Mapping[str, str] | None = None) -> tuple[float, float] | None:
    """(input, output) USD per million tokens; env LLM_PRICE_IN/LLM_PRICE_OUT override the table."""
    env = os.environ if env is None else env
    if env.get("LLM_PRICE_IN") and env.get("LLM_PRICE_OUT"):
        return float(env["LLM_PRICE_IN"]), float(env["LLM_PRICE_OUT"])
    return MODEL_PRICES_PER_MTOK.get(model)


def estimate_cost(model: str, usage: Usage, env: Mapping[str, str] | None = None) -> float | None:
    prices = prices_for(model, env)
    if prices is None:
        return None
    p_in, p_out = prices
    cost = (
        usage.input_tokens * p_in
        + usage.cache_read_tokens * p_in * CACHE_READ_MULTIPLIER
        + usage.cache_write_tokens * p_in * CACHE_WRITE_MULTIPLIER
        + usage.output_tokens * p_out
    ) / 1_000_000
    return round(cost, 6)
