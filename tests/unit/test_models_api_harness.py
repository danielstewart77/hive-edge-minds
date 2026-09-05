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
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records the URL and headers asked with, and answers with a catalog."""

    def __init__(self, recorder, payload, status=200, headers=None):
        self._recorder = recorder
        self._payload = payload
        self._status = status
        self.headers = headers

    def get(self, url, **_kwargs):
        self._recorder.append(url)
        return _Response(self._payload, self._status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install(
    monkeypatch,
    *,
    harness: str,
    payload,
    status: int = 200,
    base_url: str = "http://proxy.test:8899",
    seen_headers: list | None = None,
) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(
        models_api,
        "_provider_env",
        lambda _name: {
            "INFERENCE_PROXY_URL": base_url,
            "MIND_PROXY_KEY": "hmp-test",
        },
    )
    monkeypatch.setattr(
        models_api.runtime_config, "load_runtime", lambda _name: {"harness": harness}
    )

    def _make(**kw):
        if seen_headers is not None:
            seen_headers.append(kw.get("headers"))
        return _Session(urls, payload, status)

    monkeypatch.setattr(models_api.aiohttp, "ClientSession", _make)
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


def test_the_picker_presents_this_mind_s_own_credential(monkeypatch):
    """Requirement 4 — the listing is filtered by the key, so it must carry one.

    What makes the picker equal to the permission is that the proxy sees this
    mind's own ``hmp-`` key. An unauthenticated request is a different question
    with a different answer.
    """
    headers: list = []
    _install(
        monkeypatch, harness="claude_cli", payload={"data": []}, seen_headers=headers,
    )

    asyncio.run(models_api.build_catalog("skippy"))

    assert headers[0]["Authorization"] == "Bearer hmp-test"


def test_a_mind_asking_on_a_retired_path_is_not_shown_as_empty(monkeypatch):
    """Requirement 3, consumer half — the 410 exists to be seen.

    The proxy answers a retired listing path with 410 precisely so a stale
    client does not read a 200 as an empty catalog. Swallowing it into an
    empty list defeats the retirement and renders a working mind as one with
    no models.
    """
    _install(monkeypatch, harness="claude_cli", payload={}, status=410)

    with pytest.raises(RuntimeError) as refused:
        asyncio.run(models_api.build_catalog("skippy"))

    assert "410" in str(refused.value)


def test_an_unreachable_proxy_is_an_empty_picker_not_an_error(monkeypatch):
    """Requirement 4 — the documented failure mode, asserted.

    "Nothing offered" and "the mind is down" need different words on screen,
    which only holds if this genuinely does not raise.
    """
    _install(monkeypatch, harness="claude_cli", payload={}, status=503)

    assert asyncio.run(models_api.build_catalog("skippy")) == []


def test_a_body_that_is_not_a_catalog_does_not_escape_as_an_error(monkeypatch):
    """Requirement 4 — a reachable proxy answering wrongly is still empty.

    A captive portal or a reverse proxy's own error page arrives as a 200
    carrying something that is not a listing, and reading it as one raised
    out of the route.
    """
    _install(monkeypatch, harness="claude_cli", payload="not a catalog")

    assert asyncio.run(models_api.build_catalog("skippy")) == []


def test_a_row_with_no_usable_name_is_not_offered(monkeypatch):
    """Requirement 4 — a blank row in a picker is a model nobody can choose."""
    _install(
        monkeypatch,
        harness="claude_cli",
        payload={"data": [{"id": "   "}, {"id": "real-model"}]},
    )

    rows = asyncio.run(models_api.build_catalog("skippy"))

    assert [row["name"] for row in rows] == ["real-model"]


def test_an_openai_configured_mind_does_not_ask_for_a_doubled_version(monkeypatch):
    """Requirement 4 — the version segment belongs to the SDK, not the listing.

    ``OPENAI_BASE_URL`` ends in ``/v1`` because that is what the OpenAI client
    is given; appending the listing path to it asks for ``/v1/v1/models`` and
    the mind shows an empty picker beside a claude mind that works.
    """
    urls = _install(
        monkeypatch,
        harness="codex_cli",
        payload={"data": []},
        base_url="http://proxy.test:8899/v1",
    )

    asyncio.run(models_api.build_catalog("skippy"))

    assert urlparse(urls[0]).path == "/v1/models"
