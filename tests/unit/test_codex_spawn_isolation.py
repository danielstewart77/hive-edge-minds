"""Guards for the codex spawn console-isolation seam.

Two Windows lessons, learned in sequence on the remote Codex minds:

1. codex sharing the parent's console → a console CTRL event from the
   bash/curl/jq hook subprocess tree kills codex mid-turn with
   STATUS_CONTROL_C_EXIT (0xC000013A), surfacing as random empty replies.
2. DETACHED_PROCESS (no console at all) fixes that but makes every
   console-subsystem child allocate a fresh VISIBLE conhost window on the
   logged-in user's desktop.

CREATE_NO_WINDOW is the fix for both: codex gets a hidden console that all
descendants inherit. These tests lock in `_spawn_isolation` so a future edit
can't regress to either failure mode, and keep the POSIX new-session reap seam.
"""

from __future__ import annotations

import os
import subprocess

import mind_templates.codex_cli as codex_template

# Win32 creation-flag constants (mirrors CPython's subprocess values).
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def test_windows_isolation_hides_console_without_detaching(monkeypatch):
    monkeypatch.setattr(codex_template.os, "name", "nt", raising=False)
    kwargs = codex_template._spawn_isolation()

    # Hidden console, inherited by descendants — no CTRL kill, no conhost pop.
    assert "creationflags" in kwargs
    assert kwargs["creationflags"] & CREATE_NO_WINDOW
    assert kwargs["creationflags"] & CREATE_NEW_PROCESS_GROUP
    assert not kwargs["creationflags"] & DETACHED_PROCESS
    # start_new_session is POSIX-only and must not be passed on Windows.
    assert "start_new_session" not in kwargs


def test_posix_isolation_starts_new_session(monkeypatch):
    monkeypatch.setattr(codex_template.os, "name", "posix", raising=False)
    kwargs = codex_template._spawn_isolation()

    # New session keeps a kill/killpg reaping the node+rust tree on POSIX.
    assert kwargs == {"start_new_session": True}
    assert "creationflags" not in kwargs


def test_isolation_matches_real_platform():
    """On the host running the suite, the helper picks the right seam."""
    kwargs = codex_template._spawn_isolation()
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs == {"start_new_session": True}


def test_windows_flags_resolve_without_win32_attrs(monkeypatch):
    """The getattr fallbacks keep the Windows branch importable on POSIX,
    where subprocess lacks the Win32 creation flags."""
    monkeypatch.setattr(codex_template.os, "name", "nt", raising=False)
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    monkeypatch.delattr(subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)

    kwargs = codex_template._spawn_isolation()
    assert kwargs["creationflags"] == (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
