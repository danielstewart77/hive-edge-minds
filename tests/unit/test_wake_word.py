"""Tests for wake-word session logic."""

from voice.wake_word import WakeWordConfig, WakeWordController


def test_wake_word_triggers_and_extracts_command() -> None:
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=8.0),
        time_source=lambda: 100.0,
    )

    result = controller.process_transcript("Example turn on the office lights")

    assert result.triggered is True
    assert result.matched_phrase == "example"
    assert result.should_dispatch is True
    assert result.command_text == "turn on the office lights"
    assert result.session_active is True
    assert result.active_until == 108.0


def test_session_followup_dispatches_without_repeating_wake_word() -> None:
    clock = {"now": 10.0}
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=5.0),
        time_source=lambda: clock["now"],
    )

    controller.process_transcript("Example")
    clock["now"] = 12.0
    result = controller.process_transcript("open the garage door")

    assert result.triggered is False
    assert result.should_dispatch is True
    assert result.command_text == "open the garage door"
    assert result.session_active is True
    assert result.active_until == 17.0


def test_session_expiry_blocks_command_without_fresh_wake_word() -> None:
    clock = {"now": 1.0}
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=3.0),
        time_source=lambda: clock["now"],
    )

    controller.process_transcript("Example")
    clock["now"] = 5.1
    result = controller.process_transcript("status report")

    assert result.triggered is False
    assert result.should_dispatch is False
    assert result.command_text == ""
    assert result.session_active is False
    assert result.active_until is None


def test_cancel_phrase_closes_active_session_without_dispatch() -> None:
    clock = {"now": 10.0}
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=5.0),
        time_source=lambda: clock["now"],
    )

    controller.process_transcript("Example")
    clock["now"] = 11.0
    result = controller.process_transcript("Never mind")

    assert result.canceled is True
    assert result.matched_cancel_phrase == "never mind"
    assert result.should_dispatch is False
    assert result.command_text == ""
    assert result.session_active is False
    assert result.active_until is None


def test_wake_word_plus_cancel_phrase_closes_session_without_dispatch() -> None:
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example",), followup_window_seconds=5.0),
        time_source=lambda: 10.0,
    )

    result = controller.process_transcript("Example cancel")

    assert result.triggered is True
    assert result.canceled is True
    assert result.matched_cancel_phrase == "cancel"
    assert result.should_dispatch is False
    assert result.session_active is False


def test_longest_phrase_wins_when_aliases_overlap() -> None:
    controller = WakeWordController(
        config=WakeWordConfig(phrases=("example", "hey example"), followup_window_seconds=8.0),
        time_source=lambda: 20.0,
    )

    result = controller.process_transcript("Hey Example give me the weather")

    assert result.matched_phrase == "hey example"
    assert result.command_text == "give me the weather"


def test_update_config_rejects_invalid_values() -> None:
    controller = WakeWordController()

    try:
        controller.update_config(phrases=["   "])
    except ValueError as exc:
        assert "cannot be empty" in str(exc)
    else:
        raise AssertionError("Expected update_config to reject empty phrases")

    try:
        controller.update_config(followup_window_seconds=0)
    except ValueError as exc:
        assert "greater than zero" in str(exc)
    else:
        raise AssertionError("Expected update_config to reject zero followup window")

    try:
        controller.update_config(cancel_phrases=["   "])
    except ValueError as exc:
        assert "cannot be empty" in str(exc)
    else:
        raise AssertionError("Expected update_config to reject empty cancel phrases")
