"""Model → provider resolution.

Static aliases (sonnet/opus/haiku) map to Anthropic via ``config.models``.
Anything else falls through to the Ollama provider if one is configured.
``mind_server`` constructs the registry from ``config.yaml`` at session
spawn so the subprocess inherits the provider's env overrides
(ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, etc.).

The live-discovery model list (``list_models`` / ``_fetch_ollama_models``
+ 60 s cache) lived here before Phase B7 — it was only consumed by the
A5-deleted ``/models`` gateway endpoint. The replacement, when wired,
will live in hive-tools so that hosts running Ollama in a container, on
the host, or against an external instance all answer the same query
shape.
"""

from dataclasses import dataclass, field


@dataclass
class Provider:
    name: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    api_base: str | None = None  # used by hive-tools' future /models query


class ModelRegistry:
    def __init__(
        self,
        providers: dict[str, Provider],
        static_models: dict[str, str],
    ):
        self._providers = providers
        self._static = static_models  # {"sonnet": "anthropic", ...}

    def get_provider(self, model: str) -> Provider:
        """Resolve a model name to its provider."""
        if model in self._static:
            return self._providers[self._static[model]]
        if "ollama" in self._providers:
            return self._providers["ollama"]
        raise ValueError(f"Unknown model: {model}")
