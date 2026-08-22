"""The mind's own view of the two directories its harness reads per turn.

`skills_sync` answers a different question — repo copy against installed
copy, and the actions that move a whole skill between them. This module is
narrower and more direct: the files themselves, listed and edited in place,
in the directories the harness actually loads.

Two trees, because those are the two the harness reads and the only two
worth an editor: `skills` and `hooks`, under `$CLAUDE_CONFIG_DIR` or
`$CODEX_HOME` per harness. Nothing else on the mind's disk is reachable
from here. A path is resolved and then checked against its root, symlinks
followed — a skill symlinked to a plugin directory is ordinary, a symlink
aimed at `~/.ssh` is not, and only resolution tells them apart.

Editing is in place and immediate. A hook is re-read by the harness on
every turn and a skill on every load, so there is no deploy step to offer
and no restart to ask for. The write goes through a temporary file in the
same directory and an atomic replace, carrying the original's mode across:
a hook is executable, and a truncating write onto a `.sh` the harness is
executing right now is how a turn dies mid-flight.

Creating and deleting are deliberately absent. `skills_sync` owns whole-
directory moves; this owns the contents of files that already exist.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from skills_sync import SKILL_FILE, SkillError, SkillUnavailable, harness_directory

# The two directories a harness reads. Not a general file browser: every
# other path on the mind's disk is out of reach by construction rather than
# by a rule someone has to remember.
TREES = ("skills", "hooks")

# An editor's limit, not a storage one. Past this it is a transcript, a
# model or a database that landed in the directory by accident, and a
# textarea is the wrong tool for all three.
MAX_FILE_BYTES = 1024 * 1024

# Directories that are not the skill, they are what the skill installed. A
# real skills root on an operator mind is thousands of files and hundreds of
# megabytes, nearly all of it one skill's virtualenv — enumerating that over
# an HTTP call the console gives twelve seconds is a timeout, and nobody
# opened this to edit `libmupdf.so`. They are named in the response rather
# than dropped: a listing that quietly omits things teaches its reader to
# distrust it.
VENDOR_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", "site-packages", "__pypackages__"}
)

# A backstop for the case the exclusions miss. Also reported rather than
# silently applied, for the same reason.
MAX_LISTED_FILES = 2000

# Enough of a file to tell text from binary. Reading a megabyte of every
# row to decide whether it is editable turns a two-thousand-file listing
# into gigabytes of I/O inside the console's twenty-second timeout.
SNIFF_BYTES = 4096

# A save stages beside the target and renames onto it, which has to be the
# same filesystem. Staging at the *tree root* rather than inside the skill
# keeps a crash between the two from leaving a file that `skills_sync`
# hashes — a skill stuck reading `differs` forever, whose only offered
# remedy is the one that discards the edit that made it.
STAGING_PREFIX = ".incoming-"

# Anything older than this was left by a process that died mid-save.
STAGING_TTL_SECONDS = 3600


class FileError(SkillError):
    """A request naming something that cannot be a file under these roots."""


class FileTooLarge(FileError):
    """More than this editor will write. Not the same as absent."""


class FileUnavailable(SkillUnavailable):
    """The tree itself could not be read. Not the same as empty."""


def harness_home(harness: str) -> Path:
    """The config home this harness reads, from the environment at call time."""
    if harness_directory(harness) == "codex":
        home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    else:
        home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(home)


def tree_root(harness: str, tree: str) -> Path:
    if tree not in TREES:
        raise FileError(f"Unknown tree: {tree!r}")
    return harness_home(harness) / tree


def _resolved_root(harness: str, tree: str) -> Path:
    """The root with symlinks resolved, which is what containment is judged against."""
    return tree_root(harness, tree).resolve()


def _contained(candidate: Path, roots: tuple[Path, ...]) -> bool:
    return any(candidate == root or root in candidate.parents for root in roots)


def _containment_roots(harness: str, tree: str) -> tuple[Path, ...]:
    """Where a file of this tree is allowed to actually live.

    The tree itself, and the harness's `plugins/` directory. A skill
    installed as a symlink into `$CLAUDE_CONFIG_DIR/plugins/...` is the
    documented arrangement, and refusing it means a skill the mind runs is
    neither listable nor openable.

    `plugins/`, not the whole config home: the home also holds
    `settings.json`, and a symlink planted in a skill directory would
    otherwise make an editor for skills into an editor for the harness's
    own configuration and its tokens. A link aimed anywhere else — `/etc`,
    another user's home — is outside both roots and refused.
    """
    roots = [_resolved_root(harness, tree)]
    plugins = harness_home(harness) / "plugins"
    if plugins.exists():
        roots.append(plugins.resolve())
    return tuple(roots)


def _resolve(harness: str, tree: str, relative: str) -> Path:
    """One relative path inside a tree, or a refusal.

    Resolution happens before the containment check, so a symlink aimed out
    of the tree is refused by where it lands rather than by how it is
    spelled. `..` is rejected up front as well: a caller that sends it is
    not describing a file this API is for.
    """
    # Slashes only. Stripping whitespace as well would list a file whose
    # name has a leading or trailing space and then refuse to open it.
    text = (relative or "").strip("/")
    if not text:
        raise FileError("No file named")
    if "\\" in text or "\0" in text:
        raise FileError(f"Invalid path: {relative!r}")
    parts = [part for part in text.split("/") if part]
    if any(part in ("..", ".") for part in parts):
        raise FileError(f"Invalid path: {relative!r}")

    root = _resolved_root(harness, tree)
    candidate = (root / Path(*parts)).resolve()
    if not _contained(candidate, _containment_roots(harness, tree)):
        raise FileError(f"{relative} is outside this mind's {tree} directory")
    return candidate


def _sweep_staging(root: Path) -> None:
    """Drop staging files a dead process left behind. Best effort."""
    try:
        entries = list(root.glob(f"{STAGING_PREFIX}*"))
    except OSError:
        return
    for entry in entries:
        try:
            if time.time() - entry.stat().st_mtime > STAGING_TTL_SECONDS:
                entry.unlink()
        except OSError:
            continue


def _revision(path: Path) -> str:
    """A short hash of the file as it stands, carried by reads and by saves.

    This is what makes a save refuse rather than clobber. Two tabs, or two
    operators, or one operator who pressed "Apply repo copy" on the sync
    table above since opening the file: the second save is working from a
    buffer that no longer describes the file, and silently winning is the
    worst of the available outcomes.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _is_text(data: bytes, *, truncated: bool = False) -> bool:
    """Whether these bytes read as text.

    `truncated` says the sample stops mid-file, in which case a decode
    error in the last few bytes is a multi-byte character cut in half, not
    a binary file. Without that, whether a 24 KB prose hook is editable
    depends on whether an em-dash happens to straddle offset 4096 — and it
    would report "not editable here" in the listing while opening fine.
    """
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A UTF-8 sequence is at most four bytes, so anything starting
        # earlier than that from the end is a real decode failure.
        return truncated and exc.start >= len(data) - 3
    return True


def list_tree(harness: str, tree: str) -> dict:
    """Every file under one tree, deepest path and all, sorted by path.

    Directories are not rows — a file browser over two known roots wants the
    leaves. `__pycache__` and its `.pyc` files are included rather than
    filtered: they are genuinely in the directory, they are reported as not
    editable like any other binary, and a listing that quietly omits things
    teaches its reader to distrust it.
    """
    root = tree_root(harness, tree)
    roots = _containment_roots(harness, tree) if root.exists() else ()
    if not root.exists():
        # An absent hooks directory is an ordinary state for a mind that has
        # none. Empty and honest beats a 404 the console has to special-case.
        return {"tree": tree, "root": str(root), "exists": False, "files": []}

    rows: list[dict] = []
    omitted: list[str] = []
    outside: list[str] = []
    truncated = False
    try:
        for path in sorted(_walk(root, tree)):
            relative = path.relative_to(root)
            if relative.parts[0].startswith(STAGING_PREFIX):
                continue
            vendor = next(
                (part for part in relative.parts if part in VENDOR_DIRECTORIES), None
            )
            if vendor is not None:
                branch = Path(*relative.parts[: relative.parts.index(vendor) + 1])
                if branch.as_posix() not in omitted:
                    omitted.append(branch.as_posix())
                continue
            try:
                if not path.is_file():
                    continue
                # Followed, not trusted: a symlink out of the tree is not a
                # file of this mind's skills however it is named. Named
                # rather than dropped — a browser that hides things without
                # saying so cannot be trusted about absence, and "this skill
                # is a link to somewhere I will not follow" is a different
                # sentence from "this skill does not exist".
                if path.resolve() != path and not _contained(path.resolve(), roots):
                    if relative.as_posix() not in outside:
                        outside.append(relative.as_posix())
                    continue
                data = path.stat()
            except OSError:
                continue
            if len(rows) >= MAX_LISTED_FILES:
                # There is a further file, so the listing really is short.
                truncated = True
                break
            rows.append(
                {
                    # Posix separators whatever the host: a Windows mind
                    # would otherwise emit `memory\\SKILL.md`, which the
                    # path check refuses — every row in the skills tree is
                    # nested, so every row would be unopenable.
                    "path": relative.as_posix(),
                    "size": data.st_size,
                    "editable": _editable_reason(path, data.st_size, sniff_only=True)
                    is None,
                }
            )
    except OSError as exc:
        raise FileUnavailable(f"{root} cannot be listed: {exc}") from exc
    return {
        "tree": tree,
        "root": str(root),
        "exists": True,
        "files": rows,
        "omitted": omitted,
        "outside": outside,
        "truncated": truncated,
    }


def _walk(root: Path, tree: str):
    """What a tree contains, which is not the same question for both trees.

    `hooks` is a flat directory of scripts and the harness reads all of it,
    so it is walked whole. `skills` is a root that also holds a mind's own
    state — `.usage.json` written by the telemetry hook under a lock file,
    `.curator_state` at 0600, an `.archived` directory — none of which is a
    skill and all of which a naive walk would offer for editing. So only
    directories that actually hold a `SKILL.md` are descended into, which is
    the same population `skills_sync` reports.
    """
    if tree != "skills":
        return root.rglob("*")
    found = []
    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        raise FileUnavailable(f"{root} cannot be listed: {exc}") from exc
    for entry in entries:
        try:
            if not entry.is_dir() or not (entry / SKILL_FILE).is_file():
                continue
        except OSError:
            continue
        found.extend(entry.rglob("*"))
    return found


def _editable_reason(path: Path, size: int, *, sniff_only: bool = False) -> str | None:
    """Why this file cannot be opened in an editor, or None if it can.

    `sniff_only` is for the listing, which asks this of every row: a few
    kilobytes settles text against binary, and reading each file whole would
    turn a two-thousand-row listing into gigabytes inside the console's
    twenty-second timeout.
    """
    if size > MAX_FILE_BYTES:
        return f"{size // 1024} KB is too large to edit here"
    want = SNIFF_BYTES if sniff_only else MAX_FILE_BYTES
    try:
        with path.open("rb") as handle:
            sample = handle.read(min(size, want) or 1)
    except OSError as exc:
        return f"cannot be read: {exc}"
    if not _is_text(sample, truncated=len(sample) < size):
        return "not a text file"
    return None


def _read_text(path: Path) -> str:
    """Exactly the bytes on disk, decoded.

    `newline=""` because universal-newline translation is silent and
    lossy in both directions: a CRLF file read here and saved back
    unchanged would rewrite every line, and on a Windows-deployed mind a
    text-mode write turns an LF shell script into one that dies on its own
    shebang. `encoding` is pinned for the same reason — the locale must not
    decide whether an em-dash in a hook survives a round trip.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def read_file(harness: str, tree: str, relative: str) -> dict:
    """One file's text, or the reason it cannot be shown as text."""
    path = _resolve(harness, tree, relative)
    if path.is_dir():
        raise FileError(f"{relative} is a directory, not a file")
    if not path.is_file():
        raise FileError(f"No such file: {relative}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileUnavailable(f"{relative} cannot be read: {exc}") from exc
    reason = _editable_reason(path, size)
    if reason is not None:
        return {
            "tree": tree,
            "path": relative.strip("/"),
            "editable": False,
            "reason": reason,
            "text": None,
            "revision": None,
        }
    return {
        "tree": tree,
        "path": relative.strip("/"),
        "editable": True,
        "reason": None,
        "text": _read_text(path),
        "revision": _revision(path),
    }


class StaleWrite(SkillError):
    """The file changed under the editor between opening it and saving."""


def write_file(
    harness: str, tree: str, relative: str, text: str, revision: str | None = None
) -> dict:
    """Replace one existing file's contents, atomically, keeping its mode.

    Existing, because creating files is not what this is for — a path that
    names nothing is a typo or a stale listing, and inventing the file hides
    both. Atomically, because the harness may be executing this very hook
    while the write lands.
    """
    path = _resolve(harness, tree, relative)
    if path.is_dir():
        raise FileError(f"{relative} is a directory, not a file")
    if not path.is_file():
        raise FileError(f"No such file: {relative}")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise FileTooLarge("That is larger than this editor will write")
    current = _revision(path)
    if revision is not None and revision != current:
        raise StaleWrite(
            "This file changed since you opened it. Reopen it and reapply your "
            "edit — saving now would discard whatever changed it."
        )

    # The mode of the file as it stands, not a default: a hook that loses
    # its executable bit stops firing, silently, on the next turn.
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise FileUnavailable(f"{relative} cannot be read: {exc}") from exc

    target = path.resolve()
    root = _resolved_root(harness, tree)
    _sweep_staging(root)
    handle, staging = tempfile.mkstemp(prefix=STAGING_PREFIX, dir=str(root))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.chmod(staging, mode & 0o7777)
        os.replace(staging, target)
    except OSError as exc:
        raise FileUnavailable(f"{relative} cannot be written: {exc}") from exc
    finally:
        if Path(staging).exists():
            Path(staging).unlink(missing_ok=True)
    return {
        "tree": tree,
        "path": relative.strip("/"),
        "saved": True,
        "size": len(text.encode("utf-8")),
        # Of the bytes just written, not of a re-read. A re-read hands back
        # whatever landed *after* this save — another writer's, or an
        # "Apply repo copy" from the table above — and the editor would
        # store it as its own, so its next save would pass the staleness
        # check and clobber silently. The 409 would be defeated by its own
        # success path.
        "revision": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }


# Kept importable for symmetry with skills_sync's own helper surface.
__all__ = [
    "FileError",
    "FileTooLarge",
    "FileUnavailable",
    "StaleWrite",
    "MAX_FILE_BYTES",
    "TREES",
    "harness_home",
    "list_tree",
    "read_file",
    "tree_root",
    "write_file",
]