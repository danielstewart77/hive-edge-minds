"""Windows Task Scheduler entry point for a mind.

systemd's ``EnvironmentFile=`` (how a Linux edge mind injects ``.env``) has no
Windows equivalent, so this bootstrap loads ``.env`` into the process
environment and puts the harness binary on ``PATH``, then hands off to the
shared launcher unchanged.

The scheduled task runs this under ``pythonw.exe`` (no console window in the
logged-in user's session), so stdout/stderr are rebound to a logfile before
anything imports — otherwise the windowless interpreter's ``None`` streams
break the app's logging.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Rebind streams first: pythonw has no console, so sys.stdout/stderr are None.
_log = open(ROOT / "mind.log", "a", buffering=1, encoding="utf-8")
sys.stdout = _log
sys.stderr = _log

from dotenv import load_dotenv  # noqa: E402 - streams must be rebound first

load_dotenv(ROOT / ".env")

# Put the harness binary and the Git-for-Windows tools on PATH so both the
# harness adapter and the memory hooks resolve their dependencies. The Stop /
# UserPromptSubmit hooks are bash scripts that call bash, jq, curl and GNU
# date; Git ships all of them under usr\bin and bin. Windows' own date is not
# GNU-compatible, so Git\usr\bin must precede System32.
#
# WINDOWS_PATH_PREPEND (.env, os.pathsep-separated) carries anything
# machine-specific — e.g. a standalone codex.exe install dir.
_PATH_PREPEND = [
    p for p in os.environ.get("WINDOWS_PATH_PREPEND", "").split(os.pathsep) if p
] + [
    r"C:\Program Files\Git\usr\bin",   # bash, jq, curl, date (GNU coreutils)
    r"C:\Program Files\Git\bin",       # bash.exe, sh.exe
]
_existing = os.environ.get("PATH", "")
os.environ["PATH"] = os.pathsep.join(
    [p for p in _PATH_PREPEND if os.path.isdir(p)] + [_existing]
)

import runpy  # noqa: E402 - environment must be prepared before handoff

runpy.run_path(str(ROOT / "launch_mind_server_and_bots.py"), run_name="__main__")
