"""Tests for the wake-word desktop status window controller."""

from __future__ import annotations

from voice.wake_word_app import WakeWordAppEvent
from voice.wake_word_window import WakeWordWindowController, WakeWordWindowState


def test_controller_updates_state_from_runtime_events() -> None:
    controller = WakeWordWindowController()
    controller.state = WakeWordWindowState(running=True, status_text="Listening")

    controller._events.put(WakeWordAppEvent(kind="transcript", message="Transcript captured", transcript="Example status"))
    controller._events.put(
        WakeWordAppEvent(
            kind="dispatching",
            message="Dispatching command to Example",
            command_text="status",
            transcript="Example status",
        )
    )
    controller._events.put(
        WakeWordAppEvent(
            kind="response",
            message="Example responded",
            command_text="status",
            response_text="Systems green",
        )
    )

    events = controller.drain_events()

    assert [event.kind for event in events] == ["transcript", "dispatching", "response"]
    assert controller.state.running is True
    assert controller.state.status_text == "Responded"
    assert controller.state.last_transcript == "Example status"
    assert controller.state.last_command == "status"
    assert controller.state.last_response == "Systems green"
    assert controller.state.last_error == ""


def test_controller_marks_listener_offline_after_stop() -> None:
    controller = WakeWordWindowController()
    controller.state = WakeWordWindowState(running=True, status_text="Listening")
    controller._events.put(WakeWordAppEvent(kind="stopped", message="Wake word listener stopped"))

    controller.drain_events()

    assert controller.state.running is False
    assert controller.state.status_text == "Offline"


def test_controller_records_error_message() -> None:
    controller = WakeWordWindowController()
    controller._events.put(WakeWordAppEvent(kind="error", message="Speech recognition failed"))

    controller.drain_events()

    assert controller.state.status_text == "Error"
    assert controller.state.last_error == "Speech recognition failed"


def test_controller_mute_toggles_pause_event_and_state() -> None:
    import threading

    controller = WakeWordWindowController()
    # Not started yet — nothing to mute.
    assert controller.set_muted(True) is False

    controller._pause_event = threading.Event()
    assert controller.set_muted(True) is True
    assert controller.muted is True
    assert controller.set_muted(False) is True
    assert controller.muted is False


def test_controller_state_tracks_muted_events() -> None:
    controller = WakeWordWindowController()
    controller.state = WakeWordWindowState(running=True, status_text="Listening")

    controller._events.put(WakeWordAppEvent(kind="muted", message="Wake word listener muted"))
    controller.drain_events()
    assert controller.state.muted is True
    assert controller.state.status_text == "Muted"

    controller._events.put(WakeWordAppEvent(kind="ready", message="Wake word listener is running"))
    controller.drain_events()
    assert controller.state.muted is False
    assert controller.state.status_text == "Listening"


def test_conversation_lines_show_only_the_spoken_exchange() -> None:
    from voice.wake_word_window import conversation_lines

    dispatch = WakeWordAppEvent(
        kind="dispatching", message="Dispatching command to Example",
        transcript="example what time is it", command_text="what time is it",
    )
    response = WakeWordAppEvent(
        kind="response", message="Example responded",
        command_text="what time is it", response_text="It is noon.",
    )
    noise = WakeWordAppEvent(kind="silence", message="Listening for wake word")

    assert conversation_lines(dispatch, "Example") == ["You  >  what time is it"]
    assert conversation_lines(response, "Example") == ["Example  >  It is noon."]
    assert conversation_lines(noise, "Example") == []


def test_display_name_prefers_env(monkeypatch) -> None:
    from voice.wake_word_window import _display_name

    monkeypatch.delenv("WAKE_WORD_DISPLAY_NAME", raising=False)
    monkeypatch.setenv("MIND_NAME", "probe")
    assert _display_name() == "Probe"
    monkeypatch.setenv("WAKE_WORD_DISPLAY_NAME", "Z-Bot")
    assert _display_name() == "Z-Bot"
    monkeypatch.delenv("WAKE_WORD_DISPLAY_NAME", raising=False)
    monkeypatch.delenv("MIND_NAME", raising=False)
    assert _display_name() == "Mind"
