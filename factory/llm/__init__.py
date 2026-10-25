"""Thin LLM provider interface; one implementation selected via LLM_PROVIDER."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from factory.llm.base import LLMProvider

PROVIDERS = ("anthropic", "openai", "deepseek", "scripted")


class ProviderConfigError(ValueError):
    pass


def get_provider(env: Mapping[str, str] | None = None) -> LLMProvider:
    """Build the provider named by LLM_PROVIDER (model from LLM_MODEL, key from the environment)."""
    env = os.environ if env is None else env
    name = env.get("LLM_PROVIDER", "").strip().lower()
    model = env.get("LLM_MODEL", "").strip()
    if name == "anthropic":
        from factory.llm.anthropic import DEFAULT_MODEL, AnthropicProvider

        effort = env.get("LLM_EFFORT", "high").strip() or None
        fallbacks = env.get("LLM_ANTHROPIC_FALLBACKS", "1") != "0"
        return AnthropicProvider(model=model or DEFAULT_MODEL, effort=effort, fallbacks=fallbacks)
    if name in ("openai", "deepseek"):
        from factory.llm.openai_compat import DEEPSEEK_BASE_URL, OpenAICompatProvider

        key_var = "OPENAI_API_KEY" if name == "openai" else "DEEPSEEK_API_KEY"
        key = env.get(key_var, "")
        if not key:
            raise ProviderConfigError(f"{key_var} is not set")
        if name == "openai" and not model:
            raise ProviderConfigError("LLM_MODEL is required for LLM_PROVIDER=openai")
        return OpenAICompatProvider(
            name=name,
            model=model or "deepseek-chat",
            api_key=key,
            base_url=DEEPSEEK_BASE_URL if name == "deepseek" else None,
        )
    if name == "scripted":
        from factory.llm.scripted import ScriptedProvider

        script = env.get("LLM_SCRIPT", "")
        if not script or not Path(script).exists():
            raise ProviderConfigError("LLM_PROVIDER=scripted needs LLM_SCRIPT=<script file>")
        return ScriptedProvider(Path(script))
    raise ProviderConfigError(
        f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)} (got {name or 'nothing'!r})"
    )
