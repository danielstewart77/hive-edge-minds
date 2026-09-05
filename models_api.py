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

One endpoint carries every listing and the caller names itself on it. The
endpoint used to be the signal — a claude CLI asked on the Anthropic path, a
codex CLI on the OpenAI one — but a harness that speaks every shape has no
endpoint that identifies it, and a listing route per harness is how the same
model ends up offered to one caller and invisible to another for no reason
anybody can see. So the harness travels as a parameter, and the proxy answers
with the models that harness can address.
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

#: The one listing endpoint. Who is asking travels as ``harness``.
_LISTING_PATH = "/v1/models"

#: Statuses that mean this mind, not the proxy, is the thing that is wrong.
_CLIENT_IS_STALE = frozenset({404, 410})


def _listing_root(base_url: str) -> str:
    """The proxy's root, with any version segment the SDK owns removed.

    The variables this reads are the ones an SDK is configured with, and the
    OpenAI client is given a base URL ending in ``/v1`` while the Anthropic
    one is not. Appending the listing path to the former asks for
    ``/v1/v1/models``, so an OpenAI-configured mind shows an empty picker
    while a claude one beside it works.
    """
    root = (base_url or "").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


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
    different words on screen. A proxy that answers and refuses is the other
    case and does raise — a stale mind still asking on the retired listing
    path gets a 410, and swallowing it renders a mind with no models where
    the honest reading is a mind that needs updating.
    """
    env = _provider_env(mind_name)
    base_url = _first(_BASE_URL_VARS, env)
    key = _first(_KEY_VARS, env)
    if not base_url or not key:
        return []
    harness = str(runtime_config.load_runtime(mind_name).get("harness") or "")
    family = _harness_family(harness)
    url = f"{_listing_root(base_url)}{_LISTING_PATH}?harness={family}"
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {key}"}
        ) as session:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                status = resp.status
                if status != 200:
                    if status in _CLIENT_IS_STALE:
                        raise RuntimeError(
                            f"The inference proxy refused this mind's listing "
                            f"request with {status}. This mind is asking on a "
                            f"path the proxy has retired."
                        )
                    return []
                payload = await resp.json()
    except RuntimeError:
        raise
    except Exception:
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    # A body that is not the shape the proxy documents is a reachable proxy
    # answering wrongly, which is the empty-picker case and not the raising
    # one — a captive portal or a reverse proxy's own JSON error page both
    # land here with a 200.
    listed = payload.get("data", []) if isinstance(payload, dict) else []
    for row in listed if isinstance(listed, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("id") or "").strip()
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
