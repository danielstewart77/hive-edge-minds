"""A mind's picker asks the merged listing for its own harness.

Requirement 4, consumer half: the proxy can only filter to what this mind
can address if the mind says which harness it is. Nothing in the URL
identifies it any more, so a request that forgets the parameter gets the
union — every model, including ones this harness cannot send a request in.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

import models_api


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records the URL asked for and answers with a fixed catalog."""

    def __init__(self, recorder, payload):
        self._recorder = recorder
        self._payload = payload

    def get(self, url, **_kwargs):
        self._recorder.append(url)
        return _Response(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install(monkeypatch, *, harness: str, payload: dict) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(
        models_api,
        "_provider_env",
        lambda _name: {
            "INFERENCE_PROXY_URL": "http://proxy.test:8899",
            "MIND_PROXY_KEY": "hmp-test",
        },
    )
    monkeypatch.setattr(
        models_api.runtime_config, "load_runtime", lambda _name: {"harness": harness}
    )
    monkeypatch.setattr(
        models_api.aiohttp,
        "ClientSession",
        lambda **_kw: _Session(urls, payload),
    )
    return urls


@pytest.mark.parametrize(
    "harness,expected",
    [("claude_cli", "claude"), ("codex_cli", "codex")],
)
def test_the_picker_names_its_own_harness_on_the_merged_listing(
    monkeypatch, harness, expected
):
    urls = _install(monkeypatch, harness=harness, payload={"data": []})

    asyncio.run(models_api.build_catalog("skippy"))

    assert len(urls) == 1
    parsed = urlparse(urls[0])
    assert parsed.path == "/v1/models"
    assert parse_qs(parsed.query)["harness"] == [expected]


def test_the_picker_relays_what_the_merged_listing_returned(monkeypatch):
    """The catalog is the proxy's answer, not a locally assembled one."""
    _install(
        monkeypatch,
        harness="claude_cli",
        payload={
            "data": [
                {
                    "id": "qwen35-131k",
                    "label": "Qwen 3.5",
                    "provider": "ollama",
                    "provider_label": "Ollama",
                    "wires": ["anthropic_messages", "openai_responses"],
                }
            ]
        },
    )

    rows = asyncio.run(models_api.build_catalog("skippy"))

    assert rows == [
        {
            "name": "qwen35-131k",
            "label": "Qwen 3.5",
            "provider": "ollama",
            "provider_label": "Ollama",
        }
    ]
