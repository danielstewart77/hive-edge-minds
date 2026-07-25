"""Tests for multimodal image content block construction in mind_server.send_message.

Verifies that images passed in the request body are prepended as image content
blocks in the Claude stdin payload, while text-only messages continue to send
a single text block.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _mock_mind_env(monkeypatch):
    monkeypatch.setenv("MIND_ID", "ada")


@pytest.fixture()
def client(_mock_mind_env):
    """TestClient for mind_server with CLI-mode impl (no send attribute)."""
    with patch.dict("sys.modules", {"minds.ada.implementation": MagicMock()}):
        with patch("mind_server._setup_config_dir"):
            import importlib
            import mind_server
            importlib.reload(mind_server)
            # Force CLI path: impl must not have a `send` attribute
            mind_server.impl = MagicMock(spec=[])
            yield TestClient(mind_server.app, raise_server_exceptions=False), mind_server


def _make_proc(written: list):
    """Build a mock subprocess that captures stdin writes and yields a result event."""
    mock_stdin = MagicMock()
    mock_stdin.write = lambda data: written.append(data)
    mock_stdin.drain = AsyncMock()

    result_line = json.dumps({"type": "result", "session_id": "test-sid"}) + "\n"
    mock_stdout = AsyncMock()
    mock_stdout.readline = AsyncMock(side_effect=[result_line.encode(), b""])

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    return mock_proc


class TestSendMessageImageBlocks:
    def test_text_only_sends_single_text_block(self, client):
        """Without images the content array contains exactly one text block."""
        test_client, mind_server = client
        written = []
        mind_server._sessions["sess-text"] = {
            "proc": _make_proc(written),
            "model": "sonnet",
            "in_flight": False,
            "cancel_event": None,
        }

        resp = test_client.post(
            "/sessions/sess-text/message",
            json={"content": "Hello"},
        )

        assert resp.status_code == 200
        assert written, "Nothing was written to stdin"
        msg = json.loads(written[0].rstrip(b"\n"))
        blocks = msg["message"]["content"]
        assert blocks == [{"type": "text", "text": "Hello"}]

        mind_server._sessions.pop("sess-text", None)

    def test_image_prepended_before_text_block(self, client):
        """A single image produces [image_block, text_block] on stdin."""
        test_client, mind_server = client
        written = []
        mind_server._sessions["sess-img"] = {
            "proc": _make_proc(written),
            "model": "sonnet",
            "in_flight": False,
            "cancel_event": None,
        }

        images = [{"media_type": "image/jpeg", "data": "abc123=="}]
        resp = test_client.post(
            "/sessions/sess-img/message",
            json={"content": "What's in this photo?", "images": images},
        )

        assert resp.status_code == 200
        assert written, "Nothing was written to stdin"
        msg = json.loads(written[0].rstrip(b"\n"))
        blocks = msg["message"]["content"]

        assert len(blocks) == 2
        img_block = blocks[0]
        assert img_block["type"] == "image"
        assert img_block["source"]["type"] == "base64"
        assert img_block["source"]["media_type"] == "image/jpeg"
        assert img_block["source"]["data"] == "abc123=="
        assert blocks[1] == {"type": "text", "text": "What's in this photo?"}

        mind_server._sessions.pop("sess-img", None)

    def test_multiple_images_all_prepended(self, client):
        """Multiple images are each emitted as image blocks before the text block."""
        test_client, mind_server = client
        written = []
        mind_server._sessions["sess-multi"] = {
            "proc": _make_proc(written),
            "model": "sonnet",
            "in_flight": False,
            "cancel_event": None,
        }

        images = [
            {"media_type": "image/jpeg", "data": "img1data"},
            {"media_type": "image/png", "data": "img2data"},
        ]
        resp = test_client.post(
            "/sessions/sess-multi/message",
            json={"content": "Compare these.", "images": images},
        )

        assert resp.status_code == 200
        assert written
        msg = json.loads(written[0].rstrip(b"\n"))
        blocks = msg["message"]["content"]

        assert len(blocks) == 3
        assert blocks[0]["source"]["data"] == "img1data"
        assert blocks[1]["source"]["media_type"] == "image/png"
        assert blocks[2]["type"] == "text"

        mind_server._sessions.pop("sess-multi", None)

    def test_null_images_field_ignored(self, client):
        """Explicitly passing images=null behaves the same as omitting it."""
        test_client, mind_server = client
        written = []
        mind_server._sessions["sess-null"] = {
            "proc": _make_proc(written),
            "model": "sonnet",
            "in_flight": False,
            "cancel_event": None,
        }

        resp = test_client.post(
            "/sessions/sess-null/message",
            json={"content": "Plain text", "images": None},
        )

        assert resp.status_code == 200
        assert written
        msg = json.loads(written[0].rstrip(b"\n"))
        blocks = msg["message"]["content"]
        assert blocks == [{"type": "text", "text": "Plain text"}]

        mind_server._sessions.pop("sess-null", None)
