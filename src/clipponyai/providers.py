"""Bridge from clipponyai's provider config to open-agent-compiler presets.

Any OpenAI-compatible endpoint works — hosted (OpenAI, Anthropic's compat
endpoint, OpenRouter, Groq) or local (Ollama, vLLM, llama.cpp server). The
interactive tier of open-agent-compiler always speaks the OpenAI wire format
and resolves the API key from the environment variable the preset names.
"""

from __future__ import annotations

import json
import os

from open_agent_compiler import ModelPreset, SamplingDefaults, VariantSpec

from .config import ProviderConfig

# which of the provider's models a spec should use
FAST, SLOW, VISION = "fast", "slow", "vision"


def model_for(cfg: ProviderConfig, kind: str) -> str:
    if kind == SLOW:
        return cfg.resolved_slow_model()
    if kind == VISION:
        return cfg.resolved_vision_model()
    return cfg.fast_model


def make_preset(provider_name: str, cfg: ProviderConfig, kind: str) -> ModelPreset:
    options: dict[str, str] = {}
    if cfg.base_url:
        options["base_url"] = cfg.base_url
    if cfg.api_key_env:
        options["api_key_env"] = cfg.api_key_env
    if cfg.extra_body:
        # provider_options values must be strings; the runner JSON-decodes it
        options["extra_body"] = json.dumps(cfg.extra_body)
    modalities = ("text", "image") if kind == VISION else ("text",)
    return ModelPreset(
        name=f"{provider_name}-{kind}",
        provider=provider_name,
        model_id=model_for(cfg, kind),
        sampling=SamplingDefaults(temperature=cfg.temperature),
        input_modalities=modalities,
        provider_options=options,
    )


def make_live_profile(provider_name: str, cfg: ProviderConfig, kind: str) -> VariantSpec:
    return VariantSpec(name="live", postfix="", preset=make_preset(provider_name, cfg, kind))


def missing_api_key(cfg: ProviderConfig) -> str | None:
    """Name of the unset API-key env var, or None if the provider is usable.

    Local endpoints (explicit base_url, no api_key_env) never need a key.
    """
    if cfg.api_key_env and not os.environ.get(cfg.api_key_env):
        return cfg.api_key_env
    return None
