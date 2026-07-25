Wake Word App

Goal

Build a mind wake word app that behaves like a smart speaker control path while staying modular enough to swap in real microphone hardware, streaming speech to text, and local audio playback later.

Feature List

The app supports one or more configurable wake phrases without code edits.

It maintains a follow up session window so the user can issue a second command without repeating the wake phrase.

It normalizes transcript text before matching so punctuation and casing do not break detection.

It cleanly separates transcript intake, wake detection, and mind dispatch so new hardware clients can reuse the same control flow.

It forwards only validated commands. Wake phrase misses do nothing. Expired sessions do nothing. Empty or too short commands do nothing.

It accepts configurable cancel phrases during the active wake window, so a user can stop listening without dispatching an accidental follow up command.

It exposes a deterministic transcript driven app path for local testing now, while preserving the existing voice server HTTP contract for future always listening clients.

Implementation Plan

Phase one lives in the voice server and stays transcript first. That phase is already implemented in this repo through the wake word controller plus the status, config, and detect endpoints.

Phase two adds the app layer. That layer owns transcript ingestion, wake word evaluation, and command dispatch into the active mind through the existing gateway session contract. The first implementation uses a transcript source abstraction and a stdin runner for cheap local testing.

Phase three adds real microphone capture, voice activity detection, streaming speech to text, TTS playback, interruption handling, and device level mute semantics. That work should plug into the transcript source and dispatcher boundaries added in phase two instead of changing the wake word logic.

Phase four adds cancellation semantics to the shared wake word controller. The controller owns matching, session teardown, and result metadata. The app config reads cancel phrases from the environment and passes them into the same controller path used by the API and listener window.

What Landed

The repo now includes a wake word app module at "voice/wake_word_app.py". It provides runtime config loading, transcript source and dispatcher interfaces, a gateway backed mind dispatcher, an orchestrator that turns transcripts into dispatched commands, and a stdin runner for manual smoke tests.

The app derives default wake phrase, display name, gateway owner type, and fallback mind id from the active "MIND_NAME", so each install routes as its own mind unless explicitly overridden.

The repo also includes a small CLI entry point at "voice/run_wake_word_app.py" for manual transcript driven runs, plus unit tests that cover wake only transcripts, follow up command dispatch, and end to end app flow with a mocked dispatcher.

The wake word controller now supports cancel phrases. Defaults are "cancel", "never mind", and "stop listening". Operators can override them with "WAKE_WORD_CANCEL_PHRASES" as a comma separated list.
