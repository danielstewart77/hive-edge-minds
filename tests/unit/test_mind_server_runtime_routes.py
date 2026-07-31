"""GET/PATCH /runtime and boot-time broker self-registration.

The console configures every mind the same way — over HTTP against these
routes — whether the mind runs in this stack or on someone else's laptop.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import runtime_config


RUNTIME = """\
name: ada
mind_id: 565e5a66-d20c-4266-872a-3268c4c894fc
harness: claude_cli
provider: anthropic
default_model: sonnet
gateway_url: http://ada:8420
surfaces:
  - telegram
"""


@pytest.fixture()
def mind_files(tmp_path, monkeypatch):
    mind_dir = tmp_path / "minds" / "ada"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(RUNTIME)
    monkeypatch.setattr(runtime_config, "PROJECT_DIR", tmp_path)
    return mind_dir / "runtime.yaml"


@pytest.fixture()
def server(mind_files, monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "565e5a66-d20c-4266-872a-3268c4c894fc")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("MIND_ADMIN_TOKEN", "s3cret")
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import mind_server
            importlib.reload(mind_server)
            yield mind_server


@pytest.fixture()
def client(server):
    return TestClient(server.app, raise_server_exceptions=False)


class TestGetRuntime:
    def test_reports_the_configuration(self, client):
        body = client.get("/runtime").json()["configuration"]
        assert body["default_model"] == "sonnet"
        assert body["harness"] == "claude_cli"
        assert body["provider"] == "anthropic"

    def test_needs_no_token(self, client):
        assert client.get("/runtime").status_code == 200


class TestPatchRuntime:
    def test_writes_the_model_to_disk(self, client, mind_files):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
        assert response.json()["configuration"]["default_model"] == "opus"
        assert "default_model: opus" in mind_files.read_text()

    def test_rejects_a_missing_token(self, client, mind_files):
        response = client.patch("/runtime", json={"default_model": "opus"})
        assert response.status_code == 401
        assert "default_model: sonnet" in mind_files.read_text()

    def test_rejects_a_wrong_token(self, client, mind_files):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401
        assert "default_model: sonnet" in mind_files.read_text()

    def test_refuses_rather_than_opens_when_no_token_is_configured(
        self, client, mind_files, monkeypatch
    ):
        monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
        response = client.patch("/runtime", json={"default_model": "opus"})
        assert response.status_code == 503
        assert "default_model: sonnet" in mind_files.read_text()

    def test_rejects_a_bad_model_name(self, client, mind_files):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus; rm -rf /"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 400
        assert "default_model: sonnet" in mind_files.read_text()

    def test_rejects_an_empty_model(self, client):
        response = client.patch(
            "/runtime", json={}, headers={"Authorization": "Bearer s3cret"}
        )
        assert response.status_code == 400


class TestBrokerSelfRegistration:
    async def test_posts_runtime_yaml_to_the_broker(self, server, monkeypatch):
        monkeypatch.setenv("COMMS_URL", "http://comms:8426")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")
        posted = {}

        class _Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, json, headers, timeout):
                posted.update(url=url, payload=json, headers=headers)
                return _Response()

        with patch("aiohttp.ClientSession", _Session):
            await server._register_with_broker()

        assert posted["url"] == "http://comms:8426/broker/minds"
        assert posted["headers"]["Authorization"] == "Bearer admin"
        assert posted["payload"] == {
            "mind_id": "565e5a66-d20c-4266-872a-3268c4c894fc",
            "name": "ada",
            "gateway_url": "http://ada:8420",
            "model": "sonnet",
            "harness": "claude_cli",
        }

    async def test_registers_an_edited_model_after_a_restart(self, server, monkeypatch):
        monkeypatch.setenv("COMMS_URL", "http://comms:8426")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")
        runtime_config.update_default_model("ada", "opus")
        posted = {}

        class _Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, json, headers, timeout):
                posted.update(payload=json)
                return _Response()

        with patch("aiohttp.ClientSession", _Session):
            await server._register_with_broker()

        assert posted["payload"]["model"] == "opus"

    async def test_unreachable_broker_does_not_stop_the_mind(self, server, monkeypatch):
        monkeypatch.setenv("COMMS_URL", "http://comms:8426")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")

        class _Session:
            async def __aenter__(self):
                raise OSError("connection refused")

            async def __aexit__(self, *exc):
                return False

        with patch("aiohttp.ClientSession", _Session):
            await server._register_with_broker()  # no raise

    async def test_no_comms_configured_is_a_no_op(self, server, monkeypatch):
        monkeypatch.delenv("COMMS_URL", raising=False)
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")
        with patch("aiohttp.ClientSession", side_effect=AssertionError("should not connect")):
            await server._register_with_broker()


class TestSpawnRequiresAModel:
    def test_create_session_refuses_a_body_with_no_model(self, client):
        response = client.post(
            "/sessions",
            json={"session_id": "s1", "resume_sid": "c1"},
        )
        assert response.status_code == 400
        assert "model" in response.json()["error"]
