"""Tests for resolving a named function to real source on this mind's disk.

Every test writes its own fixture checkout and asserts against the literal
line numbers and bodies in it. Asserting against the span the resolver
itself returned would prove only that it agrees with itself — a whole-file
span, or a neighbouring function's, would satisfy that just as well.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import code_symbols


FIXTURE = '''"""Module docstring mentioning def helper in prose."""


def helper(value):
    return value + 1


def helper_neighbour(value):
    return value - 1


class Holder:
    def helper(self, value):
        return value * 2
'''


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "module.py").write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))
    return repo


def test_resolving_a_function_returns_its_real_span_and_body(checkout):
    symbol = code_symbols.resolve_symbol(str(checkout), "pkg/module.py", "helper")

    assert symbol.resolved is True
    assert (symbol.first_line, symbol.last_line) == (4, 5)
    assert symbol.source == "def helper(value):\n    return value + 1"
    assert "helper_neighbour" not in symbol.source


def test_resolving_reads_the_file_again_rather_than_a_cached_body(checkout):
    first = code_symbols.resolve_symbol(str(checkout), "pkg/module.py", "helper")
    (checkout / "pkg" / "module.py").write_text(
        FIXTURE.replace("return value + 1", "return value + 99"), encoding="utf-8"
    )

    second = code_symbols.resolve_symbol(str(checkout), "pkg/module.py", "helper")

    assert "return value + 99" in second.source
    assert second.sha != first.sha


def test_a_name_absent_from_the_file_is_reported_unresolved(checkout):
    present = code_symbols.list_symbols(str(checkout), "pkg/module.py")
    assert "helper" in present
    assert "helper_typo" not in present

    symbol = code_symbols.resolve_symbol(str(checkout), "pkg/module.py", "helper_typo")

    assert symbol.resolved is False
    assert symbol.source == ""
    assert symbol.first_line is None
    assert "helper_typo" in symbol.note


def test_a_method_is_addressed_by_its_owner_and_not_confused_with_a_function(checkout):
    method = code_symbols.resolve_symbol(
        str(checkout), "pkg/module.py", "Holder.helper"
    )

    assert method.resolved is True
    assert (method.first_line, method.last_line) == (13, 14)
    assert method.source.strip() == "def helper(self, value):\n        return value * 2".strip()


def test_a_name_defined_twice_is_reported_ambiguous_rather_than_resolved(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "twice.py").write_text(
        "def thing():\n    return 1\n\n\ndef thing():\n    return 2\n", encoding="utf-8"
    )
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))

    symbol = code_symbols.resolve_symbol(str(repo), "twice.py", "thing")

    assert symbol.resolved is False
    assert symbol.definitions == [1, 5]


def test_the_same_name_in_two_repositories_resolves_in_the_one_asked_for(
    tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "mod.py").write_text("def shared():\n    return 'first'\n", encoding="utf-8")
    (second / "mod.py").write_text(
        "def other():\n    return 0\n\n\ndef shared():\n    return 'second'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DESIGN_REPO_ROOTS", os.pathsep.join([str(first), str(second)]))

    from_first = code_symbols.resolve_symbol(str(first), "mod.py", "shared")
    from_second = code_symbols.resolve_symbol(str(second), "mod.py", "shared")

    assert from_first.first_line == 1
    assert "first" in from_first.source
    assert from_second.first_line == 5
    assert "second" in from_second.source


def test_a_repository_this_mind_was_not_given_is_refused(tmp_path, monkeypatch):
    declared = tmp_path / "declared"
    declared.mkdir()
    (declared / "mod.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "mod.py").write_text("def thing():\n    return 2\n", encoding="utf-8")
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(declared))

    assert code_symbols.resolve_symbol(str(declared), "mod.py", "thing").resolved is True
    with pytest.raises(code_symbols.SymbolError):
        code_symbols.resolve_symbol(str(elsewhere), "mod.py", "thing")


def test_a_path_escaping_the_repository_is_refused(checkout, tmp_path):
    outside = tmp_path / "secret.py"
    outside.write_text("def secret():\n    return 'no'\n", encoding="utf-8")

    with pytest.raises(code_symbols.SymbolError):
        code_symbols.resolve_symbol(str(checkout), "../secret.py", "secret")


def test_a_symlink_out_of_the_repository_is_refused(checkout, tmp_path):
    outside = tmp_path / "secret.py"
    outside.write_text("def secret():\n    return 'no'\n", encoding="utf-8")
    (checkout / "link.py").symlink_to(outside)

    with pytest.raises(code_symbols.SymbolError):
        code_symbols.resolve_symbol(str(checkout), "link.py", "secret")


def test_a_decorated_function_span_starts_at_its_decorator(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.py").write_text(
        '@app.get("/thing")\ndef thing():\n    return 1\n', encoding="utf-8"
    )
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))

    symbol = code_symbols.resolve_symbol(str(repo), "routes.py", "thing")

    assert (symbol.first_line, symbol.last_line) == (1, 3)
    assert symbol.source.startswith('@app.get("/thing")')


def test_a_file_that_does_not_parse_is_reported_rather_than_raised(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "broken.py").write_text("def thing(:\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))

    symbol = code_symbols.resolve_symbol(str(repo), "broken.py", "thing")

    assert symbol.resolved is False
    assert "parse" in symbol.note.lower()


def test_a_form_feed_above_the_target_does_not_shift_the_body(tmp_path, monkeypatch):
    """`str.splitlines` breaks on form feed; Python's line numbering does not.
    One of them above the target silently returns a different function."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ff.py").write_text(
        "def alpha():\n    return 1\n\x0c\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))

    symbol = code_symbols.resolve_symbol(str(repo), "ff.py", "beta")

    assert symbol.resolved is True
    assert symbol.source == "def beta():\n    return 2"


def test_a_unicode_line_separator_in_a_docstring_does_not_shift_the_body(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sep.py").write_text(
        'def alpha():\n    """one two"""\n    return 1\n\n\ndef beta():\n    return 2\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))

    symbol = code_symbols.resolve_symbol(str(repo), "sep.py", "beta")

    assert symbol.source == "def beta():\n    return 2"


def test_a_file_that_is_not_utf8_is_reported_rather_than_raised(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cp1252.py").write_bytes(b"# don\x92t\ndef thing():\n    return 1\n")
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(repo))

    symbol = code_symbols.resolve_symbol(str(repo), "cp1252.py", "thing")

    assert symbol.resolved is False
    assert "UTF-8" in symbol.note


def test_a_bare_repository_is_refused_when_the_mind_reads_several(
    tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "mod.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("DESIGN_REPO_ROOTS", os.pathsep.join([str(first), str(second)]))

    with pytest.raises(code_symbols.SymbolError):
        code_symbols.resolve_symbol("", "mod.py", "shared")


def test_a_bare_repository_is_allowed_when_there_is_only_one(tmp_path, monkeypatch):
    only = tmp_path / "only"
    only.mkdir()
    (only / "mod.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("DESIGN_REPO_ROOTS", str(only))

    assert code_symbols.resolve_symbol("", "mod.py", "shared").resolved is True
