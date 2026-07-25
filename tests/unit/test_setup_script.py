"""End-to-end tests for setup.sh's scaffold + installer emission.

Each test copies the repo skeleton into a tmp dir and runs setup.sh
unattended (all answers as flags, stdin closed), then asserts on the
artifacts. No sudo, no venv build (a fake .venv/bin/python3 short-circuits
it), no network.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The minimum tree setup.sh touches.
_SKELETON = [
    "setup.sh",
    ".env.example",
    "config.yaml.example",
    "souls/example.md",
    "mind_templates/claude_cli_claude.py",
    "mind_templates/codex_cli_codex.py",
]


@pytest.fixture()
def repo(tmp_path):
    for rel in _SKELETON:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst)
    (tmp_path / "minds").mkdir()
    # Short-circuit the venv build in the systemd path.
    fake_python = tmp_path / ".venv" / "bin" / "python3"
    fake_python.parent.mkdir(parents=True)
    fake_python.touch()
    fake_python.chmod(0o755)
    return tmp_path


def _run(repo: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(repo / "setup.sh"), *flags],
        cwd=repo, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_scaffolds_mind_and_stamps_env(repo):
    result = _run(repo, "--name", "atlas", "--role", "satellite",
                  "--deployment", "systemd", "--harness", "claude_cli_claude",
                  "--surfaces", "telegram", "--port", "8425")
    assert result.returncode == 0, result.stderr

    impl = (repo / "minds/atlas/implementation.py").read_text()
    assert "MIND_NAME" not in impl  # placeholder fully substituted
    assert 'mind_id: str = "atlas"' in impl

    runtime = (repo / "minds/atlas/runtime.yaml").read_text()
    assert "name: atlas" in runtime
    assert "role: satellite" in runtime
    assert "deployment: systemd" in runtime
    assert "harness: claude_cli_claude" in runtime
    assert "- telegram" in runtime

    env = (repo / ".env").read_text()
    assert "MIND_NAME=atlas" in env
    assert "MIND_SERVER_PORT=8425" in env
    # MIND_ID stamped with a real uuid, not left blank
    mind_id_line = [l for l in env.splitlines() if l.startswith("MIND_ID=")][0]
    assert len(mind_id_line.split("=", 1)[1]) == 36

    assert (repo / "souls/atlas.md").exists()


def test_systemd_emits_unit_with_repo_paths(repo):
    result = _run(repo, "--name", "atlas", "--role", "operator",
                  "--deployment", "systemd", "--harness", "claude_cli_claude",
                  "--surfaces", "none", "--port", "8421")
    assert result.returncode == 0, result.stderr
    unit = (repo / "deploy/atlas.service").read_text()
    assert f"WorkingDirectory={repo}" in unit
    assert f"EnvironmentFile={repo}/.env" in unit
    assert "launch_mind_server_and_bots.py" in unit
    assert "/home/" not in unit.split("Environment=PATH=")[0]  # no foreign homes outside PATH


def test_container_emits_compose_with_operator_host_mount(repo):
    result = _run(repo, "--name", "atlas", "--role", "operator",
                  "--deployment", "container", "--harness", "codex_cli_codex",
                  "--surfaces", "telegram", "--port", "8432")
    assert result.returncode == 0, result.stderr
    compose = (repo / "docker-compose.yml").read_text()
    assert "container_name: hive-outpost-atlas" in compose
    assert "- /:/host" in compose          # operator posture
    assert "name: hivemind" in compose


def test_container_satellite_gets_project_mount_only(repo):
    result = _run(repo, "--name", "atlas", "--role", "satellite",
                  "--deployment", "container", "--harness", "codex_cli_codex",
                  "--surfaces", "telegram", "--port", "8432")
    assert result.returncode == 0, result.stderr
    compose = (repo / "docker-compose.yml").read_text()
    assert "- /:/host" not in compose
    assert "- .:/usr/src/app:rw" in compose


def test_windows_task_emits_ps1_installer(repo):
    result = _run(repo, "--name", "atlas", "--role", "satellite",
                  "--deployment", "windows-task", "--harness", "codex_cli_codex",
                  "--surfaces", "telegram", "--port", "8433")
    assert result.returncode == 0, result.stderr
    ps1 = (repo / "deploy/setup-task.ps1").read_text()
    assert "atlasBot" in ps1
    assert "launch_windows.py" in ps1
    assert "New-ScheduledTaskTrigger -AtLogOn" in ps1


def test_rejects_bad_inputs(repo):
    assert _run(repo, "--name", "Bad Name!", "--role", "satellite",
                "--deployment", "systemd", "--harness", "claude_cli_claude",
                "--surfaces", "none", "--port", "8421").returncode != 0
    assert _run(repo, "--name", "ok", "--role", "emperor",
                "--deployment", "systemd", "--harness", "claude_cli_claude",
                "--surfaces", "none", "--port", "8421").returncode != 0
    assert _run(repo, "--name", "ok", "--role", "satellite",
                "--deployment", "cloud", "--harness", "claude_cli_claude",
                "--surfaces", "none", "--port", "8421").returncode != 0
    assert _run(repo, "--name", "ok", "--role", "satellite",
                "--deployment", "systemd", "--harness", "no_such_harness",
                "--surfaces", "none", "--port", "8421").returncode != 0
