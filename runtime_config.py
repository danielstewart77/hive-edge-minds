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

import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parent

log = logging.getLogger("hive-edge.runtime")

# The credential the gateway must present on every call it makes to this mind.
# Kept beside runtime.yaml rather than inside it: runtime.yaml is what
# `GET /runtime` serves, and a secret one allowlist edit away from being
# published is a secret waiting to be published.
SESSION_TOKEN_FILENAME = "session_token"

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


#: Fields the console may write, and the pattern each value must match. A
#: provider is a name in the proxy's providers table; a model is a deployment
#: name from that provider's listing.
WRITABLE_FIELDS = {
    "default_model": _MODEL_NAME_RE,
    "provider": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"),
}


def update_runtime_fields(mind_name: str, fields: dict[str, str]) -> dict[str, Any]:
    """Rewrite the given fields in place, atomically, preserving the rest.

    Line-level substitution rather than a YAML round-trip: dumping the parsed
    document back would strip the comments that explain each field to whoever
    opens the file next. Every field lands in one write, so a mind is never
    left holding a provider that does not host the model beside it.
    """
    if not fields:
        raise ValueError("Nothing to write")
    unknown = sorted(set(fields) - set(WRITABLE_FIELDS))
    if unknown:
        raise ValueError(f"Not a writable field: {', '.join(unknown)}")
    for field, value in fields.items():
        if not WRITABLE_FIELDS[field].fullmatch(value or ""):
            raise ValueError(f"{field} contains unsupported characters")

    path = runtime_path(mind_name)
    load_runtime(mind_name)  # reject a malformed file before touching it
    updated = path.read_text()
    for field, value in fields.items():
        updated, count = re.subn(
            rf"^{field}\s*:.*$",
            f"{field}: {value}",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise ValueError(f"Runtime configuration has no {field} field")

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
    payload = {
        "mind_id": str(loaded["mind_id"]).strip(),
        "name": str(loaded.get("name") or mind_name).strip(),
        "gateway_url": str(loaded["gateway_url"]).strip(),
        "model": str(loaded["default_model"]).strip(),
        "harness": str(loaded["harness"]).strip(),
    }
    # The admin-guarded registration this mind already performs every boot is
    # the only channel by which the gateway learns the credential. Omitted
    # when there is none, so a boot that could not read its own token file
    # does not erase the gateway's working copy.
    token = session_token(mind_name)
    if token:
        payload["session_token"] = token
    return payload


def session_token_path(mind_name: str) -> Path:
    """Where this mind keeps its own session credential."""
    return runtime_path(mind_name).parent / SESSION_TOKEN_FILENAME


def session_token(mind_name: str) -> str:
    """This mind's own session credential, minted once and kept.

    Minted rather than issued: a mind nobody provisioned still ends up with a
    credential of its own, and one taken off it opens that mind and no other.
    `MIND_SESSION_TOKEN` overrides the file for installs that inject secrets
    rather than letting the mind write them.

    Returns "" when there is none and none can be written — a read-only mind
    directory must leave the mind serving as it did before, not brick it.
    """
    injected = os.environ.get("MIND_SESSION_TOKEN", "").strip()
    if injected:
        return injected

    path = session_token_path(mind_name)
    existing = _read_token(path)
    if existing:
        return existing

    minted = secrets.token_urlsafe(32)
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another reader won the race, or left an empty file behind. Whatever
        # is there now is this mind's token.
        raced = _read_token(path)
        if raced:
            return raced
        try:
            path.write_text(minted + "\n")
            path.chmod(0o600)
        except OSError:
            log.warning("Could not write session token at %s", path)
            return ""
        return minted
    except OSError:
        log.warning("Could not create session token at %s", path)
        return ""
    with os.fdopen(handle, "w") as stream:
        stream.write(minted + "\n")
    return minted


def _read_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


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
