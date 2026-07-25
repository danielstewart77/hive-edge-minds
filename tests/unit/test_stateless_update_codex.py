"""Unit tests for the stateless update_codex tool."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(REPO_ROOT / "tools/stateless/update_codex/update_codex.py")

sys.path.insert(0, str(REPO_ROOT / "tools/stateless/update_codex"))
import update_codex  # noqa: E402


class TestStatelessUpdateCodexCLI:
    def test_test_mode_json_output(self):
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert result.returncode == 0
        assert data["updated"] is True
        assert data["install_ok"] is True

    def test_test_mode_passes_through_container(self):
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--test-mode", "--container", "hive-mind-nagatha"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["container"] == "hive-mind-nagatha"

    def test_missing_docker_binary_reports_error_without_crashing(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["update_codex.py", "--container", "hive-mind-nagatha"])
        with patch.object(update_codex, "update_codex", side_effect=FileNotFoundError("no docker binary found")):
            exit_code = update_codex.main()
        data = json.loads(capsys.readouterr().out)
        assert exit_code == 1
        assert data["install_ok"] is False
        assert "docker" in data["install_stderr"]


class TestUpdateCodexUnit:
    def test_docker_bin_prefers_host_binary(self, monkeypatch):
        monkeypatch.setattr(update_codex.os.path, "exists", lambda p: p == "/host/usr/bin/docker")
        assert update_codex._docker_bin() == "/host/usr/bin/docker"

    def test_docker_bin_falls_back_to_path(self, monkeypatch):
        monkeypatch.setattr(update_codex.os.path, "exists", lambda p: False)
        monkeypatch.setattr(update_codex.shutil, "which", lambda name: "/usr/bin/docker")
        assert update_codex._docker_bin() == "/usr/bin/docker"

    def test_docker_bin_raises_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(update_codex.os.path, "exists", lambda p: False)
        monkeypatch.setattr(update_codex.shutil, "which", lambda name: None)
        try:
            update_codex._docker_bin()
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass

    def test_exec_prefix_empty_without_container(self):
        assert update_codex._exec_prefix(None) == []

    def test_exec_prefix_wraps_docker_exec(self, monkeypatch):
        monkeypatch.setattr(update_codex, "_docker_bin", lambda: "/usr/bin/docker")
        assert update_codex._exec_prefix("hive-mind-nagatha") == [
            "/usr/bin/docker", "exec", "hive-mind-nagatha",
        ]

    def test_installed_version_parses_npm_list_json(self, monkeypatch):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"dependencies": {"@openai/codex": {"version": "1.2.3"}}}),
            stderr="",
        )
        monkeypatch.setattr(update_codex, "_run", lambda argv: fake)
        assert update_codex._installed_version([]) == "1.2.3"

    def test_installed_version_none_on_bad_json(self, monkeypatch):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="not json", stderr="boom")
        monkeypatch.setattr(update_codex, "_run", lambda argv: fake)
        assert update_codex._installed_version([]) is None

    def test_update_codex_detects_version_change(self, monkeypatch):
        calls = []

        def fake_run(argv):
            calls.append(argv)
            if "list" in argv:
                version = "1.0.0" if len(calls) == 1 else "1.1.0"
                return subprocess.CompletedProcess(
                    args=argv, returncode=0,
                    stdout=json.dumps({"dependencies": {"@openai/codex": {"version": version}}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(update_codex, "_run", fake_run)
        result = update_codex.update_codex(container=None)
        assert result["before_version"] == "1.0.0"
        assert result["after_version"] == "1.1.0"
        assert result["updated"] is True
        assert result["install_ok"] is True

    def test_update_codex_reports_install_failure(self, monkeypatch):
        def fake_run(argv):
            if "list" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0,
                    stdout=json.dumps({"dependencies": {"@openai/codex": {"version": "1.0.0"}}}),
                    stderr="",
                )
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="network unreachable")

        monkeypatch.setattr(update_codex, "_run", fake_run)
        result = update_codex.update_codex(container=None)
        assert result["install_ok"] is False
        assert result["install_stderr"] == "network unreachable"
        assert result["updated"] is False
