"""Tests for the wake-word app orchestration layer."""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace

import pytest

from voice.wake_word import WakeWordConfig, WakeWordController
from voice.wake_word_app import (
    FfmpegMicrophoneTranscriptSource,
    QueueTranscriptSource,
    WakeWordApp,
    WakeWordAppConfig,
    WakeWordAppEvent,
    _resolve_ffmpeg_path,
    _wav_rms,
)


class FakeDispatcher:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def dispatch(self, command_text: str) -> str:
        self.commands.append(command_text)
        return f"handled:{command_text}"


@pytest.mark.asyncio
async def test_wake_word_app_dispatches_after_wake_phrase() -> None:
    dispatcher = FakeDispatcher()
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=8.0),
        time_source=lambda: 100.0,
    )
    app = WakeWordApp(
        transcript_source=QueueTranscriptSource(["Example open the pod bay doors"]),
        dispatcher=dispatcher,
        controller=controller,
    )

    result = await app.run_once()

    assert result is not None
    assert result.dispatched is True
    assert result.wake_word.command_text == "open the pod bay doors"
    assert result.response_text == "handled:open the pod bay doors"
    assert dispatcher.commands == ["open the pod bay doors"]


@pytest.mark.asyncio
async def test_wake_word_app_skips_dispatch_without_active_session() -> None:
    dispatcher = FakeDispatcher()
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=3.0),
        time_source=lambda: 10.0,
    )
    app = WakeWordApp(
        transcript_source=QueueTranscriptSource(["status report"]),
        dispatcher=dispatcher,
        controller=controller,
    )

    result = await app.run_once()

    assert result is not None
    assert result.dispatched is False
    assert result.response_text is None
    assert dispatcher.commands == []


@pytest.mark.asyncio
async def test_wake_word_app_uses_followup_window_for_second_command() -> None:
    clock = {"now": 1.0}
    dispatcher = FakeDispatcher()
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=5.0),
        time_source=lambda: clock["now"],
    )
    app = WakeWordApp(
        transcript_source=QueueTranscriptSource(["Example", "open the garage"]),
        dispatcher=dispatcher,
        controller=controller,
    )

    first = await app.run_once()
    clock["now"] = 3.0
    second = await app.run_once()

    assert first is not None
    assert first.dispatched is False
    assert second is not None
    assert second.dispatched is True
    assert second.response_text == "handled:open the garage"
    assert dispatcher.commands == ["open the garage"]


def test_wake_word_app_config_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("WAKE_WORD_PHRASES", "Example, Hey Example")
    monkeypatch.setenv("WAKE_WORD_CANCEL_PHRASES", "cancel, never mind, enough")
    monkeypatch.setenv("WAKE_WORD_DISPLAY_NAME", "Example")
    monkeypatch.setenv("WAKE_WORD_FOLLOWUP_SECONDS", "12")
    monkeypatch.setenv("WAKE_WORD_MIN_COMMAND_CHARS", "4")
    monkeypatch.setenv("WAKE_WORD_OWNER_TYPE", "voice:test")
    monkeypatch.setenv("WAKE_WORD_OWNER_REF", "owner1")
    monkeypatch.setenv("WAKE_WORD_CLIENT_REF", "desk-speaker")
    monkeypatch.setenv("WAKE_WORD_MIND_ID", "example")
    monkeypatch.setenv("WAKE_WORD_COMMS_URL", "http://example.test:8420")
    monkeypatch.setenv("WAKE_WORD_BEARER_TOKEN", "secret")
    monkeypatch.setenv("WAKE_WORD_VOICE_SERVER_URL", "http://example.test:8422")
    monkeypatch.setenv("WAKE_WORD_MIC_DEVICE_NAME", "Microphone (Brio 101)")
    monkeypatch.setenv("WAKE_WORD_MIC_CHUNK_SECONDS", "4.5")
    monkeypatch.setenv("WAKE_WORD_MIC_SILENCE_RMS_THRESHOLD", "220")
    monkeypatch.setenv("WAKE_WORD_FFMPEG_PATH", "C:\\ffmpeg\\bin\\ffmpeg.exe")
    monkeypatch.setenv("WAKE_WORD_STT_TIMEOUT_SECONDS", "45")

    config = WakeWordAppConfig.from_env()

    assert config.phrases == ("Example", "Hey Example")
    assert config.cancel_phrases == ("cancel", "never mind", "enough")
    assert config.followup_window_seconds == 12.0
    assert config.min_command_chars == 4
    assert config.display_name == "Example"
    assert config.owner_type == "voice:test"
    assert config.owner_ref == "owner1"
    assert config.client_ref == "desk-speaker"
    assert config.mind_id == "example"
    assert config.comms_url == "http://example.test:8420"
    assert config.bearer_token == "secret"
    assert config.voice_server_url == "http://example.test:8422"
    assert config.microphone_device_name == "Microphone (Brio 101)"
    assert config.microphone_chunk_seconds == 4.5
    assert config.microphone_silence_rms_threshold == 220.0
    assert config.microphone_ffmpeg_path == "C:\\ffmpeg\\bin\\ffmpeg.exe"
    assert config.stt_timeout_seconds == 45.0
    assert config.wake_word_config().cancel_phrases == ("cancel", "never mind", "enough")


def test_wake_word_app_config_defaults_to_active_mind_name(monkeypatch) -> None:
    for key in (
        "WAKE_WORD_PHRASES",
        "WAKE_WORD_DISPLAY_NAME",
        "WAKE_WORD_MIND_NAME",
        "WAKE_WORD_OWNER_TYPE",
        "WAKE_WORD_MIND_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MIND_NAME", "probe")
    monkeypatch.setenv("MIND_ID", "probe-uuid")

    config = WakeWordAppConfig.from_env()

    assert config.phrases == ("probe",)
    assert config.display_name == "Probe"
    assert config.owner_type == "voice:probe"
    assert config.mind_id == "probe-uuid"


@pytest.mark.asyncio
async def test_wake_word_app_event_messages_use_mind_name() -> None:
    dispatcher = FakeDispatcher()
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("probe",), followup_window_seconds=8.0),
        time_source=lambda: 100.0,
    )
    events = []
    app = WakeWordApp(
        transcript_source=QueueTranscriptSource(["Probe status"]),
        dispatcher=dispatcher,
        controller=controller,
        event_callback=events.append,
        mind_name="Probe",
    )

    await app.run_once()

    assert [event.message for event in events] == [
        "Wake word processed",
        "Dispatching command to Probe",
        "Probe responded",
    ]


def test_wake_word_app_config_uses_voice_server_url_fallback(monkeypatch) -> None:
    monkeypatch.delenv("WAKE_WORD_VOICE_SERVER_URL", raising=False)
    monkeypatch.setenv("VOICE_SERVER_URL", "http://voice.example:8422")

    config = WakeWordAppConfig.from_env()

    assert config.voice_server_url == "http://voice.example:8422"


def _wav_bytes_for_amplitude(amplitude: int, *, frames: int = 1600) -> bytes:
    samples = [amplitude] * frames
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples))
        return buffer.getvalue()


class FakeSttClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[bytes] = []

    def transcribe_wav(self, wav_bytes: bytes) -> str:
        self.calls.append(wav_bytes)
        return self.response_text


@pytest.mark.asyncio
async def test_microphone_transcript_source_skips_silence() -> None:
    silent_wav = _wav_bytes_for_amplitude(0)
    stt_client = FakeSttClient("should not run")

    source = FfmpegMicrophoneTranscriptSource(
        device_name="Microphone (Brio 101)",
        chunk_seconds=3.0,
        silence_rms_threshold=50.0,
        ffmpeg_path="ffmpeg",
        stt_client=stt_client,
        command_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=silent_wav,
            stderr=b"",
        ),
        platform_name="Windows",
    )

    transcript = await source.next_transcript()

    assert transcript == ""
    assert stt_client.calls == []


@pytest.mark.asyncio
async def test_microphone_transcript_source_transcribes_non_silent_audio() -> None:
    spoken_wav = _wav_bytes_for_amplitude(2000)
    stt_client = FakeSttClient("Example status report")

    source = FfmpegMicrophoneTranscriptSource(
        device_name="Microphone (Brio 101)",
        chunk_seconds=3.0,
        silence_rms_threshold=50.0,
        ffmpeg_path="ffmpeg",
        stt_client=stt_client,
        command_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=spoken_wav,
            stderr=b"",
        ),
        platform_name="Windows",
    )

    transcript = await source.next_transcript()

    assert transcript == "Example status report"
    assert stt_client.calls == [spoken_wav]


@pytest.mark.asyncio
async def test_microphone_transcript_source_ignores_stt_failures() -> None:
    spoken_wav = _wav_bytes_for_amplitude(2000)

    class FailingSttClient:
        def transcribe_wav(self, wav_bytes: bytes) -> str:
            raise RuntimeError("voice-server STT request timed out")

    source = FfmpegMicrophoneTranscriptSource(
        device_name="Microphone (Brio 101)",
        chunk_seconds=3.0,
        silence_rms_threshold=50.0,
        ffmpeg_path="ffmpeg",
        stt_client=FailingSttClient(),
        command_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=spoken_wav,
            stderr=b"",
        ),
        platform_name="Windows",
    )

    transcript = await source.next_transcript()

    assert transcript == ""


@pytest.mark.asyncio
async def test_microphone_transcript_source_ignores_capture_failures() -> None:
    source = FfmpegMicrophoneTranscriptSource(
        device_name="Microphone (Brio 101)",
        chunk_seconds=3.0,
        silence_rms_threshold=50.0,
        ffmpeg_path="ffmpeg",
        stt_client=FakeSttClient("unused"),
        command_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"device missing",
        ),
        platform_name="Windows",
    )

    transcript = await source.next_transcript()

    assert transcript == ""


def test_wav_rms_reports_signal_strength() -> None:
    quiet = _wav_bytes_for_amplitude(0)
    loud = _wav_bytes_for_amplitude(2000)

    assert _wav_rms(quiet) == 0.0
    assert _wav_rms(loud) > 1000.0


def test_resolve_ffmpeg_path_finds_winget_install(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("voice.wake_word_app.shutil.which", lambda _: None)
    local_appdata = tmp_path / "LocalAppData"
    ffmpeg_path = (
        local_appdata
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg"
        / "ffmpeg-8.1.2-full_build"
        / "bin"
        / "ffmpeg.exe"
    )
    ffmpeg_path.parent.mkdir(parents=True)
    ffmpeg_path.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    resolved = _resolve_ffmpeg_path("ffmpeg")

    assert resolved == str(ffmpeg_path)


@pytest.mark.asyncio
async def test_microphone_capture_hides_console_window_on_windows(monkeypatch) -> None:
    """ffmpeg must spawn with CREATE_NO_WINDOW under a windowless parent —
    otherwise every capture chunk pops a visible conhost on the desktop."""
    import voice.wake_word_app as wake_word_app_mod

    monkeypatch.setattr(wake_word_app_mod.os, "name", "nt")
    monkeypatch.setattr(
        wake_word_app_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )

    spoken_wav = _wav_bytes_for_amplitude(2000)
    stt_client = FakeSttClient("hidden capture")
    captured_kwargs: dict = {}

    def runner(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=spoken_wav, stderr=b"")

    source = FfmpegMicrophoneTranscriptSource(
        device_name="Microphone (Brio 101)",
        chunk_seconds=3.0,
        silence_rms_threshold=50.0,
        ffmpeg_path="ffmpeg",
        stt_client=stt_client,
        command_runner=runner,
        platform_name="Windows",
    )

    transcript = await source.next_transcript()

    assert transcript == "hidden capture"
    assert captured_kwargs["creationflags"] == 0x08000000


@pytest.mark.asyncio
async def test_runner_pause_event_mutes_without_capturing(monkeypatch) -> None:
    """While paused the runner emits one muted event, captures nothing, and
    emits ready again on unmute — the standby half of the speaker UI."""
    import threading
    import voice.wake_word_app as mod

    captures: list[str] = []

    class FakeSource:
        def __init__(self, **kwargs):
            pass

        async def next_transcript(self):
            captures.append("capture")
            await asyncio.sleep(0.01)  # yield like a real mic capture does
            return ""

    class FakeDispatcherCls:
        def __init__(self, **kwargs):
            pass

        async def dispatch(self, command_text):
            return ""

    monkeypatch.setattr(mod, "FfmpegMicrophoneTranscriptSource", FakeSource)
    monkeypatch.setattr(mod, "GatewayMindDispatcher", FakeDispatcherCls)

    events: list[WakeWordAppEvent] = []
    stop = threading.Event()
    pause = threading.Event()
    pause.set()

    async def drive():
        await asyncio.sleep(0.8)
        assert captures == []  # muted: microphone untouched
        assert [e.kind for e in events].count("muted") == 1
        pause.clear()
        await asyncio.sleep(0.6)
        stop.set()

    runner = mod.run_microphone_wake_word_app_with_events(
        event_callback=events.append, stop_event=stop, pause_event=pause,
    )
    await asyncio.gather(runner, drive())

    kinds = [e.kind for e in events]
    assert kinds[0] == "ready"
    assert "muted" in kinds
    # ready emitted again after unmute (beyond the initial one)
    assert kinds.count("ready") >= 2
    assert captures, "capture loop did not resume after unmute"
    assert kinds[-1] == "stopped"
