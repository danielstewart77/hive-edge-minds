"""`GET /host` — the machine, guarded.

The reading itself is covered in `test_host_metrics.py`. What this pins is
the route: that it refuses a caller without the admin credential, that it
refuses rather than opening when no credential is configured at all, and
that what it hands back carries the host identity the console collapses
co-located minds on.

The guard matters more here than on most routes. The reply is an inventory
of a family machine — its hostname, its disk layout, every GPU in it — on a
port that answers across the LAN. The session middleware does not cover
this path (it guards `/sessions` only), so a route that forgot to call
`_authorize_admin` would ship open and look perfectly healthy.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MIND_ID", "ada")
os.environ.setdefault("MIND_NAME", "example")
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="host-route-test-"))

ADMIN_TOKEN = "not-a-real-token-host-route-test"  # secret-guard: allow


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "ada")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("MIND_ADMIN_TOKEN", ADMIN_TOKEN)
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib

            import mind_server

            importlib.reload(mind_server)
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server


def test_a_caller_with_no_credential_is_refused(client):
    http, _ = client

    assert http.get("/host").status_code == 401


def test_a_caller_with_the_wrong_credential_is_refused(client):
    http, _ = client

    response = http.get("/host", headers={"Authorization": "Bearer not-the-token"})

    assert response.status_code == 401


def test_a_mind_with_no_admin_credential_refuses_rather_than_opening(
    client, monkeypatch
):
    """The convention every other configuration route on this mind follows:
    an unestablished credential is a 503, never a route that serves the LAN
    because nobody configured the thing that was supposed to guard it."""
    http, mind_server = client
    monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(mind_server.runtime_config, "admin_token", lambda: "")

    assert http.get("/host").status_code == 503


def test_an_authorised_caller_gets_the_reading(client):
    http, _ = client

    response = http.get("/host", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})

    assert response.status_code == 200
    assert {"host", "gpu", "memory", "disk", "observed_at"} <= set(response.json())


def test_the_reading_names_the_host_the_console_collapses_on(client):
    """Requirement 25. Without this key the console lists one workstation
    twice — once for the bare-metal mind, once for the container on it."""
    http, _ = client

    body = http.get("/host", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).json()

    assert body["host"]["host_id"]
    assert "containerized" in body["host"]


def test_a_wedged_gpu_probe_does_not_take_the_route_down(client, monkeypatch):
    """A driver that hangs is a GPU state, not a 500. The console must still
    get memory and disk for a machine whose card is misbehaving."""
    http, mind_server = client
    monkeypatch.setattr(
        mind_server.host_metrics,
        "_run_nvidia_smi",
        lambda: (_ for _ in ()).throw(OSError("hung")),
    )

    response = http.get("/host", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})

    assert response.status_code == 200
    assert response.json()["gpu"]["status"] == "query_failed"
