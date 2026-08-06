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

            def post(self, url, json, headers, timeout, **kwargs):
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
        runtime_config.update_runtime_fields("ada", {"default_model": "opus"})
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

            def post(self, url, json, headers, timeout, **kwargs):
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


class _StatusResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session_returning(statuses):
    """An aiohttp.ClientSession stand-in answering from a status script.

    A status of None raises OSError instead — an unreachable broker. The
    last entry repeats once the script runs out.
    """
    script = list(statuses)
    last = script[-1] if script else 200

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json, headers, timeout, **kwargs):
            status = script.pop(0) if script else last
            if status is None:
                raise OSError("connection refused")
            return _StatusResponse(status)

    return _Session


class TestRegistrationLoop:
    """One test per shipped requirement; the loop is the boot-race fix."""

    @pytest.fixture(autouse=True)
    def _comms_env(self, monkeypatch):
        monkeypatch.setenv("COMMS_URL", "http://comms:8426")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin")

    async def test_registers_once_the_broker_comes_up_without_a_restart(self, server):
        """Req 1: boot wires the loop in; attempts fail while comms is down
        and succeed the moment it returns — waiting retry delays, not a
        restart, in between."""
        import asyncio
        from unittest.mock import AsyncMock

        # The race fix is dead code unless startup actually schedules the
        # loop — deleting the ensure_future line must fail this test.
        scheduled = AsyncMock()
        with patch.object(server, "_registration_loop", scheduled), \
                patch.object(server, "_fetch_secrets_on_startup", AsyncMock()):
            with TestClient(server.app):
                pass
        assert scheduled.called, "startup never starts the registration loop"

        outcomes = []
        sleeps = []
        real = server._register_with_broker

        async def recording_register():
            outcome = await real()
            outcomes.append(outcome)
            return outcome

        async def recording_sleep(seconds):
            sleeps.append(seconds)
            if "registered" in outcomes:
                raise asyncio.CancelledError

        with patch("aiohttp.ClientSession", _session_returning([None, None, 200])), \
                patch.object(server, "_register_with_broker", recording_register):
            with pytest.raises(asyncio.CancelledError):
                await server._registration_loop(sleep=recording_sleep)

        assert outcomes == ["retry", "retry", "registered"]
        # Retry waits between the failures, heartbeat wait after the success
        # — a loop that ignores outcomes sleeps the wrong sequence.
        assert sleeps == [1.0, 2.0, 300.0]

    async def test_retry_delays_grow_and_never_exceed_sixty_seconds(self, server):
        """Req 2: backoff doubles per failure and caps at 60s."""
        import asyncio

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 9:
                raise asyncio.CancelledError

        with patch("aiohttp.ClientSession", _session_returning([None])):
            with pytest.raises(asyncio.CancelledError):
                await server._registration_loop(sleep=fake_sleep)

        assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]

    async def test_a_rejected_registration_stops_and_is_logged_once(
        self, server, caplog
    ):
        """Req 3: a 4xx means the payload or token is wrong; retrying resends it."""
        import logging

        caplog.set_level(logging.WARNING)
        with patch("aiohttp.ClientSession", _session_returning([401])):
            await server._registration_loop(sleep=None)
            # returns rather than looping — a looping loop would hang here

        rejected = [r for r in caplog.records if "mind.register.rejected" in r.message]
        assert len(rejected) == 1

    async def test_a_broker_server_error_is_retried(self, server):
        """Req 4: a 5xx is the broker's problem, not the payload's."""
        import asyncio

        outcomes = []
        real = server._register_with_broker

        async def recording_register():
            outcome = await real()
            outcomes.append(outcome)
            return outcome

        sleeps = []

        async def recording_sleep(seconds):
            sleeps.append(seconds)
            if "registered" in outcomes:
                raise asyncio.CancelledError

        with patch("aiohttp.ClientSession", _session_returning([503, 200])), \
                patch.object(server, "_register_with_broker", recording_register):
            with pytest.raises(asyncio.CancelledError):
                await server._registration_loop(sleep=recording_sleep)

        assert outcomes == ["retry", "registered"]
        assert sleeps == [1.0, 300.0]

    async def test_the_heartbeat_reregisters_after_five_minutes(self, server):
        """Req 5: success is followed by another registration a heartbeat later."""
        import asyncio

        registrations = 0
        sleeps = []
        real = server._register_with_broker

        async def recording_register():
            nonlocal registrations
            outcome = await real()
            if outcome == "registered":
                registrations += 1
            return outcome

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if registrations >= 2:
                raise asyncio.CancelledError

        with patch("aiohttp.ClientSession", _session_returning([200])), \
                patch.object(server, "_register_with_broker", recording_register):
            with pytest.raises(asyncio.CancelledError):
                await server._registration_loop(sleep=fake_sleep)

        assert registrations == 2
        assert sleeps[0] == 300.0
