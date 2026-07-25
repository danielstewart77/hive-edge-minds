"""Wake-word session handling for voice-driven mind control.

This module provides a lightweight first implementation that operates on
transcribed text. It keeps the contract simple so a future always-listening
audio detector can call the same process_transcript entry point.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import threading
import time


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s']")


def _normalize_text(text: str) -> str:
    """Normalize text for phrase matching while preserving spoken words."""
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return _SPACE_RE.sub(" ", cleaned).strip()


def _tokenize(text: str) -> list[str]:
    normalized = _normalize_text(text)
    return normalized.split() if normalized else []


@dataclass(frozen=True)
class WakeWordConfig:
    phrases: tuple[str, ...] = ("example",)
    cancel_phrases: tuple[str, ...] = ("cancel", "never mind", "stop listening")
    followup_window_seconds: float = 8.0
    min_command_chars: int = 2


@dataclass(frozen=True)
class WakeWordResult:
    transcript: str
    normalized_transcript: str
    triggered: bool
    matched_phrase: str | None
    canceled: bool
    matched_cancel_phrase: str | None
    should_dispatch: bool
    command_text: str
    session_active: bool
    active_until: float | None

    def asdict(self) -> dict[str, object]:
        return asdict(self)


def _match_phrase(normalized_text: str, phrases: tuple[str, ...]) -> tuple[str | None, str]:
    """Return the first matched phrase and trailing command text."""
    words = normalized_text.split()
    if not words:
        return None, ""

    best_phrase: str | None = None
    best_index: int | None = None
    best_length = -1
    best_remainder = ""

    for phrase in phrases:
        phrase_words = _tokenize(phrase)
        if not phrase_words:
            continue
        phrase_len = len(phrase_words)
        for start in range(0, len(words) - phrase_len + 1):
            if words[start : start + phrase_len] != phrase_words:
                continue
            remainder = " ".join(words[start + phrase_len :]).strip()
            if phrase_len > best_length or (
                phrase_len == best_length and (best_index is None or start < best_index)
            ):
                best_phrase = " ".join(phrase_words)
                best_index = start
                best_length = phrase_len
                best_remainder = remainder
            break

    return best_phrase, best_remainder


def _matches_whole_phrase(normalized_text: str, phrases: tuple[str, ...]) -> str | None:
    """Return the normalized phrase that exactly matches the transcript."""
    for phrase in phrases:
        normalized_phrase = _normalize_text(phrase)
        if normalized_phrase and normalized_text == normalized_phrase:
            return normalized_phrase
    return None


class WakeWordController:
    """Track wake-word activation and the follow-up command window."""

    def __init__(
        self,
        config: WakeWordConfig | None = None,
        time_source: callable | None = None,
    ) -> None:
        self._config = config or WakeWordConfig()
        self._active_until: float | None = None
        self._time_source = time_source or time.monotonic
        self._lock = threading.Lock()

    @property
    def config(self) -> WakeWordConfig:
        return self._config

    def update_config(
        self,
        *,
        phrases: list[str] | tuple[str, ...] | None = None,
        cancel_phrases: list[str] | tuple[str, ...] | None = None,
        followup_window_seconds: float | None = None,
        min_command_chars: int | None = None,
    ) -> WakeWordConfig:
        new_phrases = self._config.phrases if phrases is None else tuple(
            _normalize_text(phrase) for phrase in phrases if _normalize_text(phrase)
        )
        new_cancel_phrases = self._config.cancel_phrases if cancel_phrases is None else tuple(
            _normalize_text(phrase) for phrase in cancel_phrases if _normalize_text(phrase)
        )
        new_window = (
            self._config.followup_window_seconds
            if followup_window_seconds is None
            else float(followup_window_seconds)
        )
        new_min_chars = (
            self._config.min_command_chars
            if min_command_chars is None
            else int(min_command_chars)
        )

        if not new_phrases:
            raise ValueError("Wake-word phrases cannot be empty")
        if cancel_phrases is not None and not new_cancel_phrases:
            raise ValueError("Cancel phrases cannot be empty")
        if new_window <= 0:
            raise ValueError("followup_window_seconds must be greater than zero")
        if new_min_chars < 0:
            raise ValueError("min_command_chars cannot be negative")

        with self._lock:
            self._config = WakeWordConfig(
                phrases=new_phrases,
                cancel_phrases=new_cancel_phrases,
                followup_window_seconds=new_window,
                min_command_chars=new_min_chars,
            )
            return self._config

    def get_status(self) -> dict[str, object]:
        now = self._time_source()
        with self._lock:
            active_until = self._active_until if self._is_session_active(now) else None
            if active_until is None:
                self._active_until = None
            return {
                "phrases": list(self._config.phrases),
                "cancel_phrases": list(self._config.cancel_phrases),
                "followup_window_seconds": self._config.followup_window_seconds,
                "min_command_chars": self._config.min_command_chars,
                "session_active": active_until is not None,
                "active_until": active_until,
            }

    def reset(self) -> None:
        with self._lock:
            self._active_until = None

    def process_transcript(self, transcript: str) -> WakeWordResult:
        normalized = _normalize_text(transcript)
        now = self._time_source()

        with self._lock:
            session_active = self._is_session_active(now)
            if not session_active:
                self._active_until = None

            matched_phrase, command_text = _match_phrase(normalized, self._config.phrases)
            triggered = matched_phrase is not None
            matched_cancel_phrase = _matches_whole_phrase(normalized, self._config.cancel_phrases)
            if triggered and command_text:
                matched_cancel_phrase = matched_cancel_phrase or _matches_whole_phrase(
                    command_text,
                    self._config.cancel_phrases,
                )
            canceled = matched_cancel_phrase is not None and (session_active or triggered)

            if canceled:
                self._active_until = None
                session_active = False
                command_text = ""
                should_dispatch = False
            elif triggered:
                self._active_until = now + self._config.followup_window_seconds
                session_active = True
                should_dispatch = len(command_text) >= self._config.min_command_chars
            elif session_active:
                command_text = normalized
                should_dispatch = len(command_text) >= self._config.min_command_chars
                if should_dispatch:
                    self._active_until = now + self._config.followup_window_seconds
            else:
                command_text = ""
                should_dispatch = False

            active_until = self._active_until if session_active else None
            return WakeWordResult(
                transcript=transcript,
                normalized_transcript=normalized,
                triggered=triggered,
                matched_phrase=matched_phrase,
                canceled=canceled,
                matched_cancel_phrase=matched_cancel_phrase,
                should_dispatch=should_dispatch,
                command_text=command_text,
                session_active=session_active,
                active_until=active_until,
            )

    def _is_session_active(self, now: float) -> bool:
        return self._active_until is not None and now <= self._active_until
