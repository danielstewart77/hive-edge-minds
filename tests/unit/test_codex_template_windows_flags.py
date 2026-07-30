"""Guard the codex template's Windows console isolation.

The harness must spawn with CREATE_NO_WINDOW, never DETACHED_PROCESS: a
detached codex has no console, so every console-subsystem child it spawns
(node/rust, plus each bash/jq/curl memory hook per turn) allocates a fresh
VISIBLE conhost window on the logged-in user's desktop. CREATE_NO_WINDOW
gives codex a hidden console that all descendants inherit. The functional
flag assertion runs on the Windows boxes via test_codex_implementation.py;
this source guard keeps the tracked template from regressing anywhere.
"""
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "mind_templates" / "codex_cli.py"


def test_template_spawns_with_hidden_console_on_windows():
    # The flags resolve via getattr so the helper stays callable on POSIX
    # (functional coverage lives in test_codex_spawn_isolation.py); this
    # source guard keeps the flag names themselves from regressing.
    src = TEMPLATE.read_text(encoding="utf-8")
    assert 'getattr(subprocess, "CREATE_NO_WINDOW"' in src
    assert '"DETACHED_PROCESS"' not in src
    assert 'getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"' in src


def test_template_isolates_process_group_on_posix():
    src = TEMPLATE.read_text(encoding="utf-8")
    assert '"start_new_session": True' in src
