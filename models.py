"""The provider a spawn runs against.

A mind declares its provider in its own ``runtime.yaml``; ``config.yaml``
holds the env overrides each provider needs (``ANTHROPIC_BASE_URL``,
``ANTHROPIC_AUTH_TOKEN``). ``mind_server`` pairs the two at spawn so the
subprocess inherits the right upstream.

The provider does not come from the model name. It used to: three short
aliases mapped to Anthropic and every other name fell through to Ollama,
which meant that naming a model by the deployment name the inference proxy
actually routes on — ``claude-opus-5`` rather than ``opus`` — silently
pointed a Claude mind at the local Ollama host. Which upstream serves a model
is the proxy's business, decided per request from the model name; all this
side has to know is which credential and base URL to hand the harness, and
the mind's own file is the honest answer to that.
"""

from dataclasses import dataclass, field


@dataclass
class Provider:
    name: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    api_base: str | None = None


class ProviderRegistry:
    """The providers declared in ``config.yaml``, and which one this mind uses."""

    def __init__(self, providers: dict[str, Provider], mind_provider: str):
        self._providers = providers
        self._mind_provider = mind_provider

    def get_provider(self, model: str = "") -> Provider:
        """This mind's provider. The model name is not consulted.

        Accepted and ignored so the harness call sites read the same on both
        sides of the change; a spawn asks "what upstream am I on", never "who
        owns this model".
        """
        del model
        provider = self._providers.get(self._mind_provider)
        if provider is None:
            raise ValueError(
                f"Provider {self._mind_provider!r} is not configured in config.yaml"
            )
        return provider
