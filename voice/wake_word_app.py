"""Wake-word app orchestration for a mind.

This module wires transcript ingestion, wake-word detection, and command
dispatch into a single app-layer contract. The first implementation uses a
transcript source abstraction so local stdin testing and future microphone
capture can share the same control flow.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
import wave
from array import array
from dataclasses import asdict, dataclass
from math import sqrt
from typing import Any, Callable, Protocol

from voice.wake_word import WakeWordConfig, WakeWordController, WakeWordResult

log = logging.getLogger("wake_word_app")


def _env_value(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value or default


def _display_name(name: str) -> str:
    return name.strip().title() or "Mind"


class TranscriptSource(Protocol):
    """Provides transcripts to the wake-word app."""

    async def next_transcript(self) -> str | None:
        """Return the next transcript, or None when the source is exhausted."""


class CommandDispatcher(Protocol):
    """Dispatches post-wake commands to the target mind."""

    async def dispatch(self, command_text: str) -> str:
        """Dispatch a command and return the mind's response text."""


@dataclass(frozen=True)
class WakeWordAppConfig:
    """Runtime settings for the wake-word app."""

    phrases: tuple[str, ...] = ("example",)
    cancel_phrases: tuple[str, ...] = ("cancel", "never mind", "stop listening")
    followup_window_seconds: float = 8.0
    min_command_chars: int = 2
    display_name: str = "Example"
    owner_type: str = "voice:example"
    owner_ref: str = "wake-word-user"
    client_ref: str = "wake-word-speaker"
    mind_id: str = "example"
    comms_url: str = "http://localhost:8420"
    bearer_token: str | None = None
    voice_server_url: str = "http://localhost:8422"
    microphone_device_name: str = "Microphone (Brio 101)"
    microphone_chunk_seconds: float = 3.0
    microphone_silence_rms_threshold: float = 150.0
    microphone_ffmpeg_path: str = "ffmpeg"
    stt_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "WakeWordAppConfig":
        mind_name = _env_value("WAKE_WORD_MIND_NAME", _env_value("MIND_NAME", "example"))
        raw_phrases = os.getenv("WAKE_WORD_PHRASES", mind_name)
        phrases = tuple(part.strip() for part in raw_phrases.split(",") if part.strip())
        if not phrases:
            phrases = (mind_name,)
        raw_cancel_phrases = os.getenv("WAKE_WORD_CANCEL_PHRASES", "cancel,never mind,stop listening")
        cancel_phrases = tuple(part.strip() for part in raw_cancel_phrases.split(",") if part.strip())
        if not cancel_phrases:
            cancel_phrases = ("cancel", "never mind", "stop listening")
        display_name = _display_name(os.getenv("WAKE_WORD_DISPLAY_NAME", mind_name))
        return cls(
            phrases=phrases,
            cancel_phrases=cancel_phrases,
            followup_window_seconds=float(os.getenv("WAKE_WORD_FOLLOWUP_SECONDS", "8")),
            min_command_chars=int(os.getenv("WAKE_WORD_MIN_COMMAND_CHARS", "2")),
            display_name=display_name,
            owner_type=os.getenv("WAKE_WORD_OWNER_TYPE", f"voice:{mind_name.lower()}"),
            owner_ref=os.getenv("WAKE_WORD_OWNER_REF", "wake-word-user"),
            client_ref=os.getenv("WAKE_WORD_CLIENT_REF", "wake-word-speaker"),
            mind_id=os.getenv("WAKE_WORD_MIND_ID", os.getenv("MIND_ID", mind_name.lower())),
            comms_url=os.getenv("WAKE_WORD_COMMS_URL", os.getenv("COMMS_URL", "http://localhost:8420")),
            bearer_token=os.getenv("WAKE_WORD_BEARER_TOKEN", os.getenv("COMMS_BEARER_TOKEN")),
            voice_server_url=os.getenv(
                "WAKE_WORD_VOICE_SERVER_URL",
                os.getenv("VOICE_SERVER_URL", "http://localhost:8422"),
            ),
            microphone_device_name=os.getenv("WAKE_WORD_MIC_DEVICE_NAME", "Microphone (Brio 101)"),
            microphone_chunk_seconds=float(os.getenv("WAKE_WORD_MIC_CHUNK_SECONDS", "3.0")),
            microphone_silence_rms_threshold=float(os.getenv("WAKE_WORD_MIC_SILENCE_RMS_THRESHOLD", "150")),
            microphone_ffmpeg_path=os.getenv("WAKE_WORD_FFMPEG_PATH", "ffmpeg"),
            stt_timeout_seconds=float(os.getenv("WAKE_WORD_STT_TIMEOUT_SECONDS", "30")),
        )

    def wake_word_config(self) -> WakeWordConfig:
        return WakeWordConfig(
            phrases=self.phrases,
            cancel_phrases=self.cancel_phrases,
            followup_window_seconds=self.followup_window_seconds,
            min_command_chars=self.min_command_chars,
        )


@dataclass(frozen=True)
class WakeWordAppResult:
    """Result of processing one transcript through the app."""

    transcript: str
    wake_word: WakeWordResult
    dispatched: bool
    response_text: str | None

    def asdict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["wake_word"] = self.wake_word.asdict()
        return payload


@dataclass(frozen=True)
class WakeWordAppEvent:
    """Structured event emitted by the wake-word runtime."""

    kind: str
    message: str
    transcript: str = ""
    command_text: str = ""
    response_text: str = ""
    session_active: bool = False
    triggered: bool = False


WakeWordEventCallback = Callable[[WakeWordAppEvent], None]


class QueueTranscriptSource:
    """In-memory transcript queue, mainly for tests and scripted feeds."""

    def __init__(self, transcripts: list[str] | tuple[str, ...]) -> None:
        self._queue = list(transcripts)

    async def next_transcript(self) -> str | None:
        if not self._queue:
            return None
        return self._queue.pop(0)


class StdinTranscriptSource:
    """Simple transcript source for local smoke tests."""

    async def next_transcript(self) -> str | None:
        try:
            line = await asyncio.to_thread(input, "")
        except EOFError:
            return None
        return line.strip()


class VoiceServerSttClient:
    """Send recorded WAV audio to the voice server STT endpoint."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def transcribe_wav(self, wav_bytes: bytes) -> str:
        boundary = f"----wake-word-{uuid.uuid4().hex}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("ascii"),
                b'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n',
                b"Content-Type: audio/wav\r\n\r\n",
                wav_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        request = urllib.request.Request(
            url=f"{self._base_url}/stt",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read()
        except TimeoutError as exc:
            raise RuntimeError("voice-server STT request timed out") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"voice-server STT request failed: {exc}") from exc

        import json

        data = json.loads(payload.decode("utf-8"))
        text = data.get("text", "")
        return text.strip() if isinstance(text, str) else ""


class FfmpegMicrophoneTranscriptSource:
    """Capture transcript chunks from a Windows microphone through ffmpeg."""

    def __init__(
        self,
        *,
        device_name: str,
        chunk_seconds: float,
        silence_rms_threshold: float,
        ffmpeg_path: str,
        stt_client: VoiceServerSttClient,
        event_callback: WakeWordEventCallback | None = None,
        command_runner: Any | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._device_name = device_name
        self._chunk_seconds = chunk_seconds
        self._silence_rms_threshold = silence_rms_threshold
        self._ffmpeg_path = ffmpeg_path
        self._stt_client = stt_client
        self._event_callback = event_callback
        self._uses_default_command_runner = command_runner is None
        self._command_runner = command_runner or subprocess.run
        self._platform_name = (platform_name or platform.system()).lower()

    async def next_transcript(self) -> str | None:
        try:
            self._emit_event("capture", "Capturing microphone audio")
            wav_bytes = await asyncio.to_thread(self._record_chunk)
        except RuntimeError:
            log.exception("Microphone capture failed")
            self._emit_event("error", "Microphone capture failed")
            await asyncio.sleep(1.0)
            return ""
        if not wav_bytes:
            log.info("Microphone capture returned no audio bytes")
            self._emit_event("idle", "Microphone capture returned no audio")
            return ""
        rms = _wav_rms(wav_bytes)
        log.info("Microphone chunk captured rms=%.1f threshold=%.1f", rms, self._silence_rms_threshold)
        if rms < self._silence_rms_threshold:
            log.info("Microphone chunk treated as silence")
            self._emit_event("silence", "Listening for wake word")
            return ""
        try:
            transcript = await asyncio.to_thread(self._stt_client.transcribe_wav, wav_bytes)
        except RuntimeError:
            log.exception("Voice-server transcription failed")
            self._emit_event("error", "Speech recognition failed")
            await asyncio.sleep(1.0)
            return ""
        if transcript.strip():
            log.info("Voice-server transcript=%r", transcript.strip())
            self._emit_event("transcript", "Transcript captured", transcript=transcript.strip())
        else:
            log.info("Voice-server returned empty transcript")
            self._emit_event("idle", "Speech recognition returned no text")
        return transcript.strip()

    def _emit_event(self, kind: str, message: str, *, transcript: str = "") -> None:
        if self._event_callback is None:
            return
        self._event_callback(
            WakeWordAppEvent(
                kind=kind,
                message=message,
                transcript=transcript,
            )
        )

    def _record_chunk(self) -> bytes:
        if self._platform_name != "windows":
            raise RuntimeError("ffmpeg microphone capture currently supports Windows only")
        ffmpeg_path = (
            _resolve_ffmpeg_path(self._ffmpeg_path)
            if self._uses_default_command_runner
            else self._ffmpeg_path
        )
        command = [
            ffmpeg_path,
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "dshow",
            "-i",
            f"audio={self._device_name}",
            "-t",
            f"{self._chunk_seconds:.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "pipe:1",
        ]
        run_kwargs: dict[str, Any] = {"capture_output": True, "check": False}
        if os.name == "nt":
            # The app runs under pythonw (no console); without this every
            # ffmpeg capture pops a visible conhost window on the desktop.
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = self._command_runner(command, **run_kwargs)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg microphone capture failed: {stderr}")
        return result.stdout


def _resolve_ffmpeg_path(configured_path: str) -> str:
    if os.path.isabs(configured_path) and os.path.exists(configured_path):
        return configured_path

    discovered = shutil.which(configured_path)
    if discovered:
        return discovered

    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        winget_root = os.path.join(local_appdata, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_root):
            for root, _dirs, files in os.walk(winget_root):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")

    raise RuntimeError(
        "ffmpeg executable not found; set WAKE_WORD_FFMPEG_PATH to the installed ffmpeg.exe path"
    )


class GatewayMindDispatcher:
    """Dispatch commands to a mind through the existing gateway contract."""

    def __init__(
        self,
        *,
        http: Any,
        config: WakeWordAppConfig,
    ) -> None:
        from bots.gateway_client import GatewayClient

        self._client = GatewayClient(
            http=http,
            server_url=config.comms_url,
            owner_type=config.owner_type,
            mind_id=config.mind_id,
            bearer_token=config.bearer_token,
        )
        self._owner_ref = config.owner_ref
        self._client_ref = config.client_ref

    async def dispatch(self, command_text: str) -> str:
        return await self._client.query(
            user_id=self._owner_ref,
            client_ref=self._client_ref,
            prompt=command_text,
        )


class WakeWordApp:
    """Coordinates transcript intake, wake detection, and mind dispatch."""

    def __init__(
        self,
        *,
        transcript_source: TranscriptSource,
        dispatcher: CommandDispatcher,
        controller: WakeWordController | None = None,
        event_callback: WakeWordEventCallback | None = None,
        mind_name: str = "Example",
    ) -> None:
        self._transcript_source = transcript_source
        self._dispatcher = dispatcher
        self._controller = controller or WakeWordController()
        self._event_callback = event_callback
        self._mind_name = mind_name

    @property
    def controller(self) -> WakeWordController:
        return self._controller

    async def process_transcript(self, transcript: str) -> WakeWordAppResult:
        wake_word = self._controller.process_transcript(transcript)
        response_text: str | None = None
        dispatched = False
        log.info(
            "Wake-word processed transcript=%r triggered=%s dispatch=%s command=%r session_active=%s",
            transcript,
            wake_word.triggered,
            wake_word.should_dispatch,
            wake_word.command_text,
            wake_word.session_active,
        )
        self._emit_event(
            WakeWordAppEvent(
                kind="wake-word-processed",
                message="Wake word processed",
                transcript=transcript,
                command_text=wake_word.command_text or "",
                session_active=wake_word.session_active,
                triggered=wake_word.triggered,
            )
        )

        if wake_word.should_dispatch and wake_word.command_text:
            self._emit_event(
                WakeWordAppEvent(
                    kind="dispatching",
                    message=f"Dispatching command to {self._mind_name}",
                    transcript=transcript,
                    command_text=wake_word.command_text,
                    session_active=wake_word.session_active,
                    triggered=wake_word.triggered,
                )
            )
            response_text = await self._dispatcher.dispatch(wake_word.command_text)
            dispatched = True
            log.info("%s dispatch response length=%d", self._mind_name, len(response_text or ""))
            self._emit_event(
                WakeWordAppEvent(
                    kind="response",
                    message=f"{self._mind_name} responded",
                    transcript=transcript,
                    command_text=wake_word.command_text,
                    response_text=response_text or "",
                    session_active=wake_word.session_active,
                    triggered=wake_word.triggered,
                )
            )

        return WakeWordAppResult(
            transcript=transcript,
            wake_word=wake_word,
            dispatched=dispatched,
            response_text=response_text,
        )

    def _emit_event(self, event: WakeWordAppEvent) -> None:
        if self._event_callback is None:
            return
        self._event_callback(event)

    async def run_once(self) -> WakeWordAppResult | None:
        transcript = await self._transcript_source.next_transcript()
        if transcript is None:
            return None
        return await self.process_transcript(transcript)

    async def run_forever(self) -> list[WakeWordAppResult]:
        results: list[WakeWordAppResult] = []
        while True:
            result = await self.run_once()
            if result is None:
                return results
            results.append(result)


def _wav_rms(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        if not frames:
            return 0.0
        if wav_file.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM WAV input")
        samples = array("h")
        samples.frombytes(frames)
        if not samples:
            return 0.0
        mean_square = sum(sample * sample for sample in samples) / len(samples)
        return sqrt(mean_square)


async def run_stdin_wake_word_app() -> list[WakeWordAppResult]:
    """Run the wake-word app against stdin transcripts until EOF."""

    import aiohttp

    config = WakeWordAppConfig.from_env()
    controller = WakeWordController(config=config.wake_word_config())

    async with aiohttp.ClientSession() as http:
        dispatcher = GatewayMindDispatcher(http=http, config=config)
        app = WakeWordApp(
            transcript_source=StdinTranscriptSource(),
            dispatcher=dispatcher,
            controller=controller,
            mind_name=config.display_name,
        )
        return await app.run_forever()


async def run_microphone_wake_word_app() -> list[WakeWordAppResult]:
    """Run the wake-word app against live microphone transcripts forever."""
    return await run_microphone_wake_word_app_with_events()


async def run_microphone_wake_word_app_with_events(
    *,
    event_callback: WakeWordEventCallback | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> list[WakeWordAppResult]:
    """Run the microphone wake-word app with optional runtime event hooks.

    While ``pause_event`` is set the listener is muted: the microphone is
    not captured and nothing dispatches, but the worker stays alive so
    unmuting is instant. Emits one ``muted`` event on entering the state
    and one ``ready`` on leaving it.
    """

    import aiohttp

    config = WakeWordAppConfig.from_env()
    controller = WakeWordController(config=config.wake_word_config())
    transcript_source = FfmpegMicrophoneTranscriptSource(
        device_name=config.microphone_device_name,
        chunk_seconds=config.microphone_chunk_seconds,
        silence_rms_threshold=config.microphone_silence_rms_threshold,
        ffmpeg_path=config.microphone_ffmpeg_path,
        stt_client=VoiceServerSttClient(
            base_url=config.voice_server_url,
            timeout_seconds=config.stt_timeout_seconds,
        ),
        event_callback=event_callback,
    )

    async with aiohttp.ClientSession() as http:
        dispatcher = GatewayMindDispatcher(http=http, config=config)
        app = WakeWordApp(
            transcript_source=transcript_source,
            dispatcher=dispatcher,
            controller=controller,
            event_callback=event_callback,
            mind_name=config.display_name,
        )
        if event_callback is not None:
            event_callback(
                WakeWordAppEvent(
                    kind="ready",
                    message="Wake word listener is running",
                )
            )
        results: list[WakeWordAppResult] = []
        was_paused = False
        while True:
            if stop_event is not None and stop_event.is_set():
                if event_callback is not None:
                    event_callback(
                        WakeWordAppEvent(
                            kind="stopped",
                            message="Wake word listener stopped",
                        )
                    )
                return results
            if pause_event is not None and pause_event.is_set():
                if not was_paused:
                    was_paused = True
                    if event_callback is not None:
                        event_callback(
                            WakeWordAppEvent(
                                kind="muted",
                                message="Wake word listener muted",
                            )
                        )
                await asyncio.sleep(0.3)
                continue
            if was_paused:
                was_paused = False
                if event_callback is not None:
                    event_callback(
                        WakeWordAppEvent(
                            kind="ready",
                            message="Wake word listener is running",
                        )
                    )
            result = await app.run_once()
            if result is None:
                if event_callback is not None:
                    event_callback(
                        WakeWordAppEvent(
                            kind="stopped",
                            message="Wake word listener finished",
                        )
                    )
                return results
            results.append(result)
