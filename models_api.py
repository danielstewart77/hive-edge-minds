"""What models this mind may run, answered by the mind itself.

The console needs a model picker per mind, and the honest source is the mind's
own credential. This mind holds its own ``hmp-`` client key on the inference
proxy, and the proxy filters its listing by that key and by the harness the
listing was asked on — so a model that does not appear here is a model this
mind would be refused if it asked. The picker and the permission are the same
fact.

The listing is relayed, not assembled. The proxy owns the providers table, so
each row already names the upstream hosting it, and a provider added there
becomes selectable here with no code change.

Which endpoint carries the listing is decided by the harness and nothing else:
a claude CLI speaks Anthropic Messages and a codex CLI the Responses shape, so
the endpoint a listing is requested on is what tells the proxy who is asking.
"""

from __future__ import annotations

import os

import aiohttp

import runtime_config

_TIMEOUT = aiohttp.ClientTimeout(total=6)

# An explicit proxy URL wins over the harness variable that happens to point at
# the same place, because a codex mind has no ANTHROPIC_BASE_URL at all — it
# carries its provider in CODEX_HOME's config.toml, which is not readable as an
# environment variable.
_BASE_URL_VARS = ("INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL")
_KEY_VARS = ("MIND_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")

#: The listing endpoint each harness may address.
_LISTING_PATH = {"claude": "/v1/anthropic/models", "codex": "/v1/models"}


def _harness_family(harness: str) -> str:
    return "claude" if str(harness or "").startswith("claude") else "codex"


def _provider_env(mind_name: str) -> dict[str, str]:
    """The env a spawn would run under: config.yaml's provider block, then ours.

    A bare-metal mind's proxy credential lives in the provider block its spawns
    already apply, not in the service unit's environment — so reading only the
    process env would report an empty picker on exactly the deployment this
    file exists to cover.
    """
    merged: dict[str, str] = {}
    try:
        from config import config as _config

        runtime = runtime_config.load_runtime(mind_name)
        declared = _config.providers.get(str(runtime.get("provider") or "")) or {}
        overrides = declared.get("env", {}) if isinstance(declared, dict) else {}
        merged.update({str(k): str(v) for k, v in overrides.items()})
    except Exception:
        pass
    merged.update({k: v for k, v in os.environ.items() if v})
    return merged


def _first(names: tuple[str, ...], env: dict[str, str]) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


async def build_catalog(mind_name: str) -> list[dict]:
    """Every model this mind may be pointed at, as the proxy reports it.

    An unreachable proxy yields an empty list rather than raising: the console
    distinguishes "nothing offered" from "the mind is down", and the two need
    different words on screen.
    """
    env = _provider_env(mind_name)
    base_url = _first(_BASE_URL_VARS, env)
    key = _first(_KEY_VARS, env)
    if not base_url or not key:
        return []
    harness = str(runtime_config.load_runtime(mind_name).get("harness") or "")
    url = f"{base_url.rstrip('/')}{_LISTING_PATH[_harness_family(harness)]}"
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {key}"}
        ) as session:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
    except Exception:
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for row in payload.get("data", []):
        name = str(row.get("id") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        provider = str(row.get("provider") or row.get("owned_by") or "")
        rows.append(
            {
                "name": name,
                "label": str(row.get("label") or name),
                "provider": provider,
                "provider_label": str(row.get("provider_label") or provider),
            }
        )
    return rows
