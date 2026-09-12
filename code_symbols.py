"""Resolving a named function to the source actually on this machine's disk.

Page two of a design session may not propose a test against a function
nobody has looked at. The console cannot look: a mind on another machine has
no bind mount to offer, and the console's own container holds no checkout of
anything. So the mind resolves, exactly as it already reports its own skills,
files, models and host — one code path for a container in the stack, a
bare-metal mind here, and a mind on a laptop across the LAN.

What comes back is the real body, its path and its line span, plus a
fingerprint of those bytes. The fingerprint is what makes the snapshot
honest later: a file edited after publication shows as drifted rather than
leaving the page displaying old source under a live label.

Resolution is by parse, not by search. A regex for `def name` matches the
word in a docstring, in a comment, and in a string, and it cannot tell a
method on one class from a method on another. A name defined twice in one
file is reported ambiguous rather than resolved to whichever came first,
because a confident wrong line span is worse than an honest refusal.
"""

from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_SOURCE_BYTES = 400_000


class SymbolError(Exception):
    """A resolution request that cannot be answered as asked."""


@dataclass
class Symbol:
    """One resolution attempt, answered or refused."""

    name: str
    repo: str
    path: str
    resolved: bool = False
    kind: str = "existing"
    first_line: int | None = None
    last_line: int | None = None
    source: str = ""
    sha: str = ""
    note: str = ""
    definitions: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "repo": self.repo,
            "path": self.path,
            "resolved": self.resolved,
            "kind": self.kind,
            "first_line": self.first_line,
            "last_line": self.last_line,
            "source": self.source,
            "sha": self.sha,
            "note": self.note,
            "definitions": list(self.definitions),
        }


def repo_roots() -> list[Path]:
    """The checkouts this mind will read for a design session.

    Declared rather than discovered. Without a declared set, a repo path
    arriving on an HTTP request is a path traversal with extra steps, and
    this route returns file contents.
    """
    declared = os.getenv("DESIGN_REPO_ROOTS", "")
    roots = [Path(part).expanduser() for part in declared.split(os.pathsep) if part.strip()]
    if not roots:
        project = os.getenv("HIVE_PROJECT_DIR", "")
        if project:
            roots = [Path(project)]
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve(strict=True))
        except OSError:
            continue
    return resolved


def resolve_repo(repo: str) -> Path:
    """The declared checkout this request names, or a refusal.

    Matched on the *resolved* path, so a symlink pointing out of the
    declared set is refused rather than followed — the same rule the file
    editor had to learn, and for the same reason: a skill legitimately
    carries symlinks, so a lexical check admits the one aimed at `~/.ssh`.
    """
    roots = repo_roots()
    if not roots:
        raise SymbolError("This mind has no design repository roots configured")
    wanted = (repo or "").strip()
    if not wanted:
        return roots[0]
    candidate = Path(wanted).expanduser()
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise SymbolError(f"No such repository: {repo}") from exc
    for root in roots:
        if candidate == root:
            return candidate
    raise SymbolError(f"Repository is not one this mind will read: {repo}")


def _contained(root: Path, relative: str) -> Path:
    target = (root / relative).expanduser()
    try:
        target = target.resolve(strict=True)
    except OSError as exc:
        raise SymbolError(f"No such file: {relative}") from exc
    if target != root and root not in target.parents:
        raise SymbolError(f"File is outside the repository: {relative}")
    if not target.is_file():
        raise SymbolError(f"Not a file: {relative}")
    return target


def _read(target: Path) -> str:
    if target.stat().st_size > MAX_SOURCE_BYTES:
        raise SymbolError(f"File is too large to parse: {target.name}")
    # newline="" so a CRLF checkout round-trips as itself; universal-newline
    # translation would change the bytes the fingerprint is taken over.
    with open(target, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _definitions(tree: ast.AST, name: str) -> list[ast.AST]:
    """Every definition of a name, addressed bare or as `Owner.member`.

    Walked rather than read off the module body: a function defined inside a
    class, or under an `if` that the import-time branch takes, is still a
    function a test can import and a requirement can be about.
    """
    owner, _, member = name.rpartition(".")
    found: list[ast.AST] = []
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if child.name != member:
                continue
            if owner:
                if not isinstance(parent, ast.ClassDef) or parent.name != owner:
                    continue
            elif not isinstance(parent, ast.Module):
                # A bare name means module scope. Letting it also match a
                # method of the same name makes every `get`, `run` or
                # `handle` ambiguous in any file holding a class, and the
                # caller has `Owner.member` when it wants the method.
                continue
            found.append(child)
    return found


def _span(node: ast.AST, text: str) -> tuple[int, int, str]:
    first = getattr(node, "lineno", 1)
    decorators = getattr(node, "decorator_list", []) or []
    for decorator in decorators:
        first = min(first, getattr(decorator, "lineno", first))
    last = getattr(node, "end_lineno", first)
    lines = text.splitlines()
    body = "\n".join(lines[first - 1 : last])
    return first, last, body


def resolve_symbol(repo: str, path: str, name: str) -> Symbol:
    """Find one named function on disk and hand back what is actually there.

    Never raises for a name that is simply absent: "this function does not
    exist" is an answer page two has to render, and a 500 in its place reads
    to the console as a mind that is down.
    """
    root = resolve_repo(repo)
    symbol = Symbol(name=name, repo=str(root), path=path)
    target = _contained(root, path)
    text = _read(target)
    try:
        tree = ast.parse(text, filename=str(target))
    except SyntaxError as exc:
        symbol.note = f"Could not parse {path}: {exc.msg} at line {exc.lineno}"
        return symbol

    found = _definitions(tree, name)
    if not found:
        symbol.note = f"{name} is not defined in {path}"
        return symbol
    if len(found) > 1:
        symbol.definitions = [getattr(node, "lineno", 0) for node in found]
        symbol.note = (
            f"{name} is defined {len(found)} times in {path}"
            f" (lines {', '.join(str(line) for line in symbol.definitions)})"
        )
        return symbol

    first, last, body = _span(found[0], text)
    symbol.resolved = True
    symbol.first_line = first
    symbol.last_line = last
    symbol.source = body
    symbol.sha = fingerprint(body)
    symbol.definitions = [first]
    return symbol


def list_symbols(repo: str, path: str) -> list[str]:
    """Every function and class a file defines, for a caller checking a name."""
    root = resolve_repo(repo)
    target = _contained(root, path)
    tree = ast.parse(_read(target), filename=str(target))
    names: list[str] = []
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if isinstance(parent, ast.ClassDef):
                    names.append(f"{parent.name}.{child.name}")
                else:
                    names.append(child.name)
    return sorted(set(names))
