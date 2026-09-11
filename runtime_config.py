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
import time
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


class SessionTokenUnavailable(ValueError):
    """This mind cannot establish the credential its session routes require.

    A `ValueError` so the boot registration loop treats it as a retryable
    payload problem rather than a crash: the mind is still reachable, it just
    refuses every session call until whatever is wrong with its own directory
    is fixed.
    """


# How long an empty token file can plausibly be mid-write. Past this the
# process that created it is gone and the file is reclaimed, rather than
# stalling every later request on a write that will never land.
_RACE_WINDOW_S = 1.0
_RACE_PAUSE_S = 0.02

# One read per process, not one per request. The middleware asks on every
# `/sessions` call, and the broker only learns a token at boot anyway, so a
# value that changed under a running mind could not be published to anyone.
_token_cache: dict[str, str] = {}


def session_token_path(mind_name: str) -> Path:
    """Where this mind keeps its own session credential."""
    return runtime_path(mind_name).parent / SESSION_TOKEN_FILENAME


def session_token(mind_name: str) -> str:
    """This mind's own session credential, minted once and kept.

    Minted rather than issued: a mind nobody provisioned still ends up with a
    credential of its own, and one taken off it opens that mind and no other.
    `MIND_SESSION_TOKEN` overrides the file for installs that inject secrets
    rather than letting the mind write them.

    Raises `SessionTokenUnavailable` when no credential can be established.
    It never returns "" for that case: a mind that cannot read its own token
    would otherwise serve its session routes — `attach-pty` among them — open
    to the LAN, while the gateway went on presenting a credential nobody
    checked and every surface stayed green.
    """
    injected = os.environ.get("MIND_SESSION_TOKEN", "").strip()
    if injected:
        return injected

    cached = _token_cache.get(mind_name)
    if cached:
        return cached

    token = _mint_or_read_token(session_token_path(mind_name))
    _token_cache[mind_name] = token
    return token


def _mint_or_read_token(path: Path) -> str:
    existing = _read_token(path)
    if existing:
        return existing

    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _adopt_or_reclaim(path)
    except OSError as exc:
        raise SessionTokenUnavailable(f"cannot write {path}: {exc}") from exc

    minted = secrets.token_urlsafe(32)
    with os.fdopen(handle, "w") as stream:
        stream.write(minted + "\n")
    return minted


def _adopt_or_reclaim(path: Path) -> str:
    """Resolve an empty token file: someone mid-write, or someone who died.

    `O_EXCL` creates the file before its winner writes into it, so an empty
    file can mean a write still in flight — and minting a second token over
    that would leave the winner enforcing a credential that exists nowhere.
    It can equally mean a process that was killed in the microseconds between
    the create and the write, which leaves a zero-byte file that no amount of
    waiting will fill.

    The file's own age separates them, and it always resolves: inside the
    window this waits in short hops, and the moment the file is older than the
    window the mint that made it is gone and the file is reclaimed. So the cost
    is bounded by the window once — never the old behaviour, which was a full
    second of the event loop (shared here with the surface bots and the pty
    pumps) on *every* request, forever, for a file only `rm` could fix.
    """
    while True:
        adopted = _read_token(path)
        if adopted:
            return adopted
        try:
            age = time.time() - path.stat().st_mtime
        except OSError as exc:
            raise SessionTokenUnavailable(f"cannot stat {path}: {exc}") from exc
        if age > _RACE_WINDOW_S:
            # Nobody is coming. Reclaim it in place, keeping the inode so a
            # concurrent reader holding it open sees the token rather than a
            # file that vanished under them.
            minted = secrets.token_urlsafe(32)
            try:
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(minted + "\n")
                os.chmod(path, 0o600)
            except OSError as exc:
                raise SessionTokenUnavailable(
                    f"cannot reclaim empty {path}: {exc}"
                ) from exc
            return minted
        time.sleep(_RACE_PAUSE_S)


def _read_token(path: Path) -> str:
    """The credential on disk, or "" when the file is not there at all.

    A file that exists and cannot be read is not an absent one. Folding the
    two together is how a mind serves every session route open because a
    migration chowned its own directory — so only genuine absence returns "".
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise SessionTokenUnavailable(f"cannot read {path}: {exc}") from exc


def tokens_match(presented: str, expected: str) -> bool:
    """Constant-time compare of two credentials, on bytes.

    `compare_digest` raises `TypeError` on a `str` holding anything outside
    ASCII, and the presented value is a raw client-supplied header — so on
    `str` a single stray byte is a 500 instead of a clean 401, and on the
    WebSocket handshake the gateway reads that 500 as "this mind has no
    terminal route" and sends the operator off to rebuild an image.
    """
    return secrets.compare_digest(
        presented.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


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
