"""Default data paths for stateless tools resolve inside the repo, not a container."""

import argparse
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _far_future(reminders):
    return datetime(2099, 1, 1, 9, 0, tzinfo=reminders.TZ)


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name,relpath,attr,env_var,filename",
    [
        (
            "reminders_tool",
            "tools/stateless/reminders/reminders.py",
            "_DEFAULT_DB",
            "REMINDERS_DB_PATH",
            "reminders.db",
        ),
        (
            "notify_tool",
            "tools/stateless/notify/notify.py",
            "_DEFAULT_ALERT_FILE",
            "ALERT_FILE_PATH",
            "alerts.log",
        ),
    ],
)
def test_default_path_is_repo_local(monkeypatch, name, relpath, attr, env_var, filename):
    monkeypatch.delenv(env_var, raising=False)
    # The tools compute the repo root with a `parents[3]` hop count; this test
    # computes it independently from its own location. They agree only while
    # the tool sits where the count expects, so moving it — which would
    # silently write the db outside the checkout rather than raise — fails
    # here. Where that root happens to be is not the point: a container
    # install whose checkout is bind-mounted at /usr/src/app is as correct as
    # a bare-metal one under $HOME.
    default = getattr(_load(name, relpath), attr)
    assert default == str(REPO_ROOT / "data" / filename)


@pytest.mark.parametrize(
    "name,relpath,attr,env_var",
    [
        ("reminders_tool", "tools/stateless/reminders/reminders.py", "_DEFAULT_DB", "REMINDERS_DB_PATH"),
        ("notify_tool", "tools/stateless/notify/notify.py", "_DEFAULT_ALERT_FILE", "ALERT_FILE_PATH"),
    ],
)
def test_env_var_overrides_default(monkeypatch, tmp_path, name, relpath, attr, env_var):
    override = str(tmp_path / "elsewhere.dat")
    monkeypatch.setenv(env_var, override)
    assert getattr(_load(name, relpath), attr) == override


def test_reminders_round_trips_against_default_db(monkeypatch, tmp_path, capsys):
    """The default db path is usable as-is: nested dirs are created and written."""
    monkeypatch.setenv("REMINDERS_DB_PATH", str(tmp_path / "nested" / "reminders.db"))
    reminders = _load("reminders_tool", "tools/stateless/reminders/reminders.py")
    monkeypatch.setattr(reminders, "_parse_when", lambda *_a, **_k: _far_future(reminders))

    args = argparse.Namespace(
        message="sever the spoke",
        when="2099-01-01 09:00",
        test_mode=True,
        db_path=reminders._DEFAULT_DB,
    )
    assert reminders.cmd_set(args) == 0
    assert json.loads(capsys.readouterr().out)["set"] is True
    assert os.path.exists(reminders._DEFAULT_DB)

    assert reminders.cmd_list(argparse.Namespace(db_path=reminders._DEFAULT_DB)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["message"] for r in listed["reminders"]] == ["sever the spoke"]
