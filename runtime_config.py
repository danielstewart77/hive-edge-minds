"""The mind's `runtime.yaml` — read at every boot, writable at runtime.

`minds/<name>/runtime.yaml` is the durable truth about what a mind is. The
broker's `minds` row is a cache of it: the mind re-registers from this file
on every start, so a rebuilt broker database, a redeploy onto a fresh
volume, or a hand-edit of the file all converge on the file's values rather
than on whatever was true at install time.

Editing goes the other way through `mind_server`'s `PATCH /runtime`, which
lands here — the console writes the file first, then refreshes the broker
row, so the two can't diverge across a restart.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parent

# Same shape the console validates against: an alias (`opus`), an Ollama tag
# (`qwen3:30b-a3b-instruct-2507-q4_K_M`), or a vendor id (`gpt-5.4`).
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_MIND_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")

# What `GET /runtime` is willing to say about a mind. runtime.yaml holds no
# secrets today, but it is reachable over the LAN and an allowlist keeps a
# future field from leaking by default.
PUBLIC_FIELDS = (
    "name",
    "mind_id",
    "description",
    "profile",
    "role",
    "deployment",
    "harness",
    "provider",
    "default_model",
    "mind_server_port",
    "gateway_url",
    "surfaces",
    "soul_file",
)


def runtime_path(mind_name: str) -> Path:
    """Path to a mind's runtime.yaml. Raises on a name that isn't one."""
    if not _MIND_NAME_RE.fullmatch(mind_name or ""):
        raise ValueError("Invalid mind name")
    return PROJECT_DIR / "minds" / mind_name / "runtime.yaml"


def load_runtime(mind_name: str) -> dict[str, Any]:
    """This mind's runtime.yaml as a dict. Raises if absent or malformed."""
    path = runtime_path(mind_name)
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise ValueError(f"No runtime configuration at {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Runtime configuration is invalid: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Runtime configuration must be a mapping")
    return loaded


def public_runtime(mind_name: str) -> dict[str, Any]:
    """The allowlisted view of runtime.yaml served to the console."""
    loaded = load_runtime(mind_name)
    return {k: loaded[k] for k in PUBLIC_FIELDS if k in loaded}


def update_default_model(mind_name: str, model: str) -> dict[str, Any]:
    """Rewrite `default_model` in place, atomically, preserving the rest.

    A line-level substitution rather than a YAML round-trip: dumping the
    parsed document back would strip the comments that explain each field to
    whoever opens the file next.
    """
    if not _MODEL_NAME_RE.fullmatch(model or ""):
        raise ValueError("Model name contains unsupported characters")
    path = runtime_path(mind_name)
    load_runtime(mind_name)  # reject a malformed file before touching it
    text = path.read_text()
    updated, count = re.subn(
        r"^default_model\s*:.*$",
        f"default_model: {model}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("Runtime configuration has no default_model field")

    fd, temporary = tempfile.mkstemp(prefix="runtime-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return load_runtime(mind_name)


def registration_payload(mind_name: str) -> dict[str, str]:
    """The broker registration this mind's runtime.yaml describes."""
    loaded = load_runtime(mind_name)
    missing = [
        field
        for field in ("mind_id", "gateway_url", "default_model", "harness")
        if not str(loaded.get(field) or "").strip()
    ]
    if missing:
        raise ValueError(f"runtime.yaml is missing: {', '.join(missing)}")
    return {
        "mind_id": str(loaded["mind_id"]).strip(),
        "name": str(loaded.get("name") or mind_name).strip(),
        "gateway_url": str(loaded["gateway_url"]).strip(),
        "model": str(loaded["default_model"]).strip(),
        "harness": str(loaded["harness"]).strip(),
    }


def admin_token() -> str:
    """Bearer accepted on this mind's config-write route.

    A dedicated `MIND_ADMIN_TOKEN` when the install has one; otherwise the
    gateway's admin bearer, which the console already holds. No token
    configured means the write route refuses rather than opens.
    """
    return (
        os.environ.get("MIND_ADMIN_TOKEN")
        or os.environ.get("COMMS_ADMIN_BEARER_TOKEN")
        or ""
    )
