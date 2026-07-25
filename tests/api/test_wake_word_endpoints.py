"""API tests for wake-word endpoints."""

import sys
from unittest.mock import MagicMock, patch

import pytest


def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


_NEED_PYDANTIC_MOCK = not _can_import("pydantic")


@pytest.fixture(autouse=True)
def _mock_voice_deps(monkeypatch):
    """Mock heavy deps before importing voice_server."""
    np_mock = MagicMock()
    np_mock.float32 = "float32"
    np_mock.ndarray = type("ndarray", (), {})
    monkeypatch.setitem(sys.modules, "numpy", np_mock)

    torch_mock = MagicMock()
    torch_mock.cuda.is_available.return_value = False
    monkeypatch.setitem(sys.modules, "torch", torch_mock)
    monkeypatch.setitem(sys.modules, "torchaudio", MagicMock())
    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())
    monkeypatch.setitem(sys.modules, "soundfile", MagicMock())

    chatterbox_mod = MagicMock()
    monkeypatch.setitem(sys.modules, "chatterbox", chatterbox_mod)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", chatterbox_mod.tts)

    if _NEED_PYDANTIC_MOCK:
        pydantic_mock = MagicMock()
        pydantic_mock.BaseModel = type("BaseModel", (), {})
        monkeypatch.setitem(sys.modules, "pydantic", pydantic_mock)
        monkeypatch.setitem(sys.modules, "pydantic_core", MagicMock())
        monkeypatch.setitem(sys.modules, "fastapi", MagicMock())
        monkeypatch.setitem(sys.modules, "fastapi.responses", MagicMock())

    for mod_name in list(sys.modules.keys()):
        if "voice_server" in mod_name or "voice.voice_server" in mod_name:
            del sys.modules[mod_name]

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")


@pytest.fixture
def client():
    with patch("ctypes.CDLL", side_effect=OSError("no GPU")):
        for mod_name in list(sys.modules.keys()):
            if "voice_server" in mod_name or "voice.voice_server" in mod_name:
                del sys.modules[mod_name]
        from starlette.testclient import TestClient
        from voice.voice_server import app

        return TestClient(app, raise_server_exceptions=False)


pytestmark = pytest.mark.skipif(
    _NEED_PYDANTIC_MOCK,
    reason="pydantic_core native lib unavailable; FastAPI app cannot be created",
)


def test_wake_word_status_endpoint_returns_defaults(client) -> None:
    resp = client.get("/wake-word")

    assert resp.status_code == 200
    data = resp.json()
    assert data["phrases"] == ["example"]
    assert data["cancel_phrases"] == ["cancel", "never mind", "stop listening"]
    assert data["session_active"] is False


def test_wake_word_config_endpoint_updates_runtime_state(client) -> None:
    resp = client.post(
        "/wake-word/config",
        json={
            "phrases": ["Example", "Hey Example"],
            "cancel_phrases": ["Cancel", "Enough"],
            "followup_window_seconds": 12,
            "min_command_chars": 1,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["phrases"] == ["example", "hey example"]
    assert data["cancel_phrases"] == ["cancel", "enough"]
    assert data["followup_window_seconds"] == 12.0
    assert data["min_command_chars"] == 1


def test_wake_word_detect_endpoint_dispatches_command(client) -> None:
    config_resp = client.post(
        "/wake-word/config",
        json={
            "phrases": ["Example"],
            "followup_window_seconds": 8,
            "min_command_chars": 2,
        },
    )
    assert config_resp.status_code == 200

    resp = client.post("/wake-word/detect", json={"transcript": "Example start the coffee maker"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["triggered"] is True
    assert data["should_dispatch"] is True
    assert data["command_text"] == "start the coffee maker"


def test_wake_word_detect_endpoint_cancels_active_session(client) -> None:
    config_resp = client.post(
        "/wake-word/config",
        json={
            "phrases": ["Example"],
            "cancel_phrases": ["Never mind"],
            "followup_window_seconds": 8,
            "min_command_chars": 2,
        },
    )
    assert config_resp.status_code == 200

    wake_resp = client.post("/wake-word/detect", json={"transcript": "Example"})
    assert wake_resp.status_code == 200

    resp = client.post("/wake-word/detect", json={"transcript": "Never mind"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["canceled"] is True
    assert data["matched_cancel_phrase"] == "never mind"
    assert data["should_dispatch"] is False
    assert data["session_active"] is False


def test_wake_word_config_endpoint_rejects_empty_phrases(client) -> None:
    resp = client.post(
        "/wake-word/config",
        json={
            "phrases": ["   "],
            "followup_window_seconds": 8,
            "min_command_chars": 2,
        },
    )

    assert resp.status_code == 400
    assert "cannot be empty" in resp.json()["detail"]
