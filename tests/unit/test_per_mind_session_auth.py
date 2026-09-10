"""This mind's own credential, and the session routes it guards.

hive-comms presents this mind's session token on every call it makes here.
The mind mints the token once, keeps it beside its runtime configuration, and
publishes it only through the admin-guarded registration it already performs
on every boot — so a token taken off this host opens this host and no other.
"""

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
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
def mind_dir(tmp_path, monkeypatch):
    directory = tmp_path / "minds" / "ada"
    directory.mkdir(parents=True)
    (directory / "runtime.yaml").write_text(RUNTIME)
    monkeypatch.setattr(runtime_config, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("MIND_SESSION_TOKEN", raising=False)
    return directory


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "path": "/sessions"})


# ---------------------------------------------------------------------------
# R3 — the mind's credential is its own to make and its own to keep
# ---------------------------------------------------------------------------
class TestMintingTheToken:
    def test_mints_one_when_there_is_none(self, mind_dir):
        token = runtime_config.session_token("ada")
        assert token
        assert (mind_dir / "session_token").read_text().strip() == token

    def test_only_the_mind_can_read_the_file(self, mind_dir):
        runtime_config.session_token("ada")
        assert (mind_dir / "session_token").stat().st_mode & 0o777 == 0o600

    def test_the_same_token_comes_back_on_every_later_ask(self, mind_dir):
        first = runtime_config.session_token("ada")
        # A restart is a fresh read of the same directory and nothing else.
        assert runtime_config.session_token("ada") == first
        assert runtime_config.session_token("ada") == first

    def test_an_injected_token_overrides_the_file(self, mind_dir, monkeypatch):
        runtime_config.session_token("ada")
        monkeypatch.setenv("MIND_SESSION_TOKEN", "from-the-env")
        assert runtime_config.session_token("ada") == "from-the-env"

    def test_an_unwritable_directory_yields_no_token_rather_than_raising(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(runtime_config, "PROJECT_DIR", tmp_path / "nowhere")
        monkeypatch.delenv("MIND_SESSION_TOKEN", raising=False)
        assert runtime_config.session_token("ada") == ""


class TestTheTokenIsNotServed:
    def test_the_runtime_view_does_not_carry_it(self, mind_dir):
        token = runtime_config.session_token("ada")
        view = runtime_config.public_runtime("ada")
        assert "session_token" not in view
        assert token not in str(view)

    def test_the_registration_payload_does_carry_it(self, mind_dir):
        payload = runtime_config.registration_payload("ada")
        assert payload["session_token"] == runtime_config.session_token("ada")

    def test_a_mind_with_no_token_registers_without_the_field(self, mind_dir):
        with patch.object(runtime_config, "session_token", return_value=""):
            payload = runtime_config.registration_payload("ada")
        assert "session_token" not in payload


# ---------------------------------------------------------------------------
# The server that enforces it
# ---------------------------------------------------------------------------
@pytest.fixture()
def server(mind_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("MIND_ID", "565e5a66-d20c-4266-872a-3268c4c894fc")
    monkeypatch.setenv("MIND_NAME", "ada")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("MIND_ADMIN_TOKEN", "the-admin-token")
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import mind_server
            importlib.reload(mind_server)
            yield mind_server


@pytest.fixture()
def client(server):
    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture()
def token(mind_dir):
    return runtime_config.session_token("ada")


# ---------------------------------------------------------------------------
# R6 / R7 / R8 — what the guard admits
# ---------------------------------------------------------------------------
class TestTheSessionGuard:
    def test_the_right_token_is_admitted(self, server, token):
        headers = {"Authorization": f"Bearer {token}"}
        assert server._authorize_session(_request(headers)) is None

    def test_no_credential_is_refused(self, server, token):
        denied = server._authorize_session(_request())
        assert isinstance(denied, JSONResponse)
        assert denied.status_code == 401

    def test_the_wrong_credential_is_refused(self, server, token):
        headers = {"Authorization": "Bearer not-this-mind's-token"}
        denied = server._authorize_session(_request(headers))
        assert denied is not None
        assert denied.status_code == 401

    def test_the_admin_token_also_opens_a_session_route(self, server, token):
        headers = {"Authorization": "Bearer the-admin-token"}
        assert server._authorize_session(_request(headers)) is None

    def test_a_subprotocol_credential_is_admitted(self, server, token):
        """A browser attaching directly cannot set a header."""
        headers = {"Sec-WebSocket-Protocol": f"bearer.{token}"}
        assert server._authorize_session(_request(headers)) is None

    def test_a_mind_holding_no_token_serves_as_it_always_did(self, server):
        """The rollout: a mind that cannot write one is reachable, not dark."""
        with patch.object(runtime_config, "session_token", return_value=""):
            assert server._authorize_session(_request()) is None
            assert server._authorize_session(
                _request({"Authorization": "Bearer anything-at-all"})
            ) is None


class TestTheGuardOnRealRoutes:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/sessions"),
            ("post", "/sessions"),
            ("post", "/sessions/abc/message"),
            ("post", "/sessions/abc/interrupt"),
            ("post", "/sessions/abc/release"),
            ("post", "/sessions/abc/rotate-pty"),
            ("delete", "/sessions/abc"),
        ],
    )
    def test_every_session_route_refuses_an_unauthenticated_caller(
        self, client, token, method, path
    ):
        assert getattr(client, method)(path).status_code == 401

    def test_the_listing_answers_the_mind_s_own_token(self, client, token):
        response = client.get(
            "/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200

    def test_a_terminal_attach_without_a_credential_is_refused(self, client, token):
        """401, not the 403 a pre-accept close would send — which is also what
        a mind with no terminal route at all answers."""
        from starlette.testclient import WebSocketDenialResponse

        with pytest.raises(WebSocketDenialResponse) as refused:
            with client.websocket_connect(
                "/sessions/abc/attach-pty?resume_sid=conv-1&model=sonnet"
            ):
                pass
        assert refused.value.status_code == 401

    def test_a_terminal_attach_with_the_token_is_admitted(self, client, token):
        """Admitted past the guard — the spawn beyond it is stubbed, so what
        this proves is that the credential is not what stops it."""
        from starlette.testclient import WebSocketDenialResponse

        try:
            with client.websocket_connect(
                "/sessions/abc/attach-pty?resume_sid=conv-1&model=sonnet",
                headers={"Authorization": f"Bearer {token}"},
            ):
                pass
        except WebSocketDenialResponse as refused:  # pragma: no cover
            pytest.fail(f"the mind's own token was refused: {refused.status_code}")
        except Exception:
            pass  # accepted, then failed further in on the stubbed harness


# ---------------------------------------------------------------------------
# R10 — the config surface is separate in both directions
# ---------------------------------------------------------------------------
class TestTheConfigSurfaceIsSeparate:
    def test_the_session_token_does_not_unlock_the_config_routes(self, client, token):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    def test_the_admin_token_still_writes_the_config(self, client, token):
        response = client.patch(
            "/runtime",
            json={"default_model": "opus"},
            headers={"Authorization": "Bearer the-admin-token"},
        )
        assert response.status_code == 200

    def test_the_guard_leaves_the_public_runtime_read_alone(self, client, token):
        assert client.get("/runtime").status_code == 200


# ---------------------------------------------------------------------------
# R9 — refusing a terminal attach says which kind of no it is
# ---------------------------------------------------------------------------
class TestRefusingAWebsocket:
    async def test_a_denial_response_is_preferred(self, server):
        sent = []

        class Socket:
            async def send_denial_response(self, response):
                sent.append(response)

        denial = JSONResponse({"error": "unauthorized"}, status_code=401)
        await server._refuse_session_websocket(Socket(), denial)
        assert sent == [denial]

    async def test_a_server_without_the_extension_falls_back_to_a_close(self, server):
        closed = {}

        class Socket:
            async def send_denial_response(self, response):
                raise RuntimeError("extension unsupported")

            async def close(self, code, reason):
                closed.update(code=code, reason=reason)

        await server._refuse_session_websocket(
            Socket(), JSONResponse({}, status_code=401)
        )
        assert closed["code"] == 4401
