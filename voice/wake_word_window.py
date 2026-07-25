"""Native desktop smart-speaker window for a mind's wake-word listener.

The kid-facing surface on the boys' machines: turn the listener on and
off, mute it to a standby "ready" state, and watch the conversation
stream live. Titled per mind via MIND_NAME.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import scrolledtext
from typing import Callable

from voice.wake_word_app import WakeWordAppEvent, run_microphone_wake_word_app_with_events


def _display_name() -> str:
    return os.getenv("WAKE_WORD_DISPLAY_NAME", os.getenv("MIND_NAME", "Mind")).strip().title() or "Mind"


@dataclass(frozen=True)
class WakeWordWindowState:
    """Current operator-facing status for the wake-word listener."""

    running: bool = False
    muted: bool = False
    status_text: str = "Offline"
    last_transcript: str = ""
    last_command: str = ""
    last_response: str = ""
    last_error: str = ""


class WakeWordWindowController:
    """Thread-safe controller that bridges runtime events into UI state."""

    def __init__(self) -> None:
        self.state = WakeWordWindowState()
        self._events: queue.Queue[WakeWordAppEvent] = queue.Queue()
        self._stop_event: threading.Event | None = None
        self._pause_event: threading.Event | None = None
        self._worker: threading.Thread | None = None

    def start(self) -> bool:
        if self.running:
            return False
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        self.state = replace(
            self.state,
            running=True,
            muted=False,
            status_text="Starting",
            last_error="",
        )
        self._events.put(WakeWordAppEvent(kind="starting", message="Wake word listener starting"))
        return True

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def muted(self) -> bool:
        return self._pause_event is not None and self._pause_event.is_set()

    def stop(self) -> bool:
        if self._stop_event is None:
            return False
        self._stop_event.set()
        self._events.put(WakeWordAppEvent(kind="stopping", message="Wake word listener stopping"))
        return True

    def set_muted(self, muted: bool) -> bool:
        """Mute (standby) or unmute the running listener."""
        if self._pause_event is None:
            return False
        if muted:
            self._pause_event.set()
        else:
            self._pause_event.clear()
        return True

    def drain_events(self) -> list[WakeWordAppEvent]:
        events: list[WakeWordAppEvent] = []
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return events
            self.state = self._apply_event(self.state, event)
            events.append(event)

    def close(self) -> None:
        self.stop()
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    def _run_worker(self) -> None:
        assert self._stop_event is not None
        try:
            asyncio.run(
                run_microphone_wake_word_app_with_events(
                    event_callback=self._events.put,
                    stop_event=self._stop_event,
                    pause_event=self._pause_event,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive safety path
            self._events.put(WakeWordAppEvent(kind="error", message=f"Wake word listener crashed: {exc}"))
        finally:
            self._events.put(WakeWordAppEvent(kind="stopped", message="Wake word listener stopped"))

    @staticmethod
    def _apply_event(state: WakeWordWindowState, event: WakeWordAppEvent) -> WakeWordWindowState:
        running = state.running
        muted = state.muted
        status_text = state.status_text
        last_transcript = state.last_transcript
        last_command = state.last_command
        last_response = state.last_response
        last_error = state.last_error

        if event.kind in {"starting", "capture", "silence", "ready", "muted", "transcript", "wake-word-processed", "dispatching", "response"}:
            running = True
        if event.kind in {"stopping", "stopped", "error"} and event.message.startswith("Wake word listener"):
            running = event.kind != "stopped"

        if event.kind == "starting":
            status_text = "Starting"
        elif event.kind in {"ready", "silence"}:
            status_text = "Listening"
            muted = False
        elif event.kind == "muted":
            status_text = "Muted"
            muted = True
        elif event.kind == "capture":
            status_text = "Capturing"
        elif event.kind == "transcript":
            status_text = "Heard speech"
        elif event.kind == "dispatching":
            status_text = "Thinking"
        elif event.kind == "response":
            status_text = "Responded"
        elif event.kind == "stopping":
            status_text = "Stopping"
        elif event.kind == "stopped":
            status_text = "Offline"
            running = False
            muted = False
        elif event.kind == "error":
            status_text = "Error"

        if event.transcript:
            last_transcript = event.transcript
        if event.command_text:
            last_command = event.command_text
        if event.response_text:
            last_response = event.response_text
        if event.kind == "error":
            last_error = event.message
        elif event.kind in {"ready", "starting", "silence", "response", "stopped"}:
            last_error = ""

        return WakeWordWindowState(
            running=running,
            muted=muted,
            status_text=status_text,
            last_transcript=last_transcript,
            last_command=last_command,
            last_response=last_response,
            last_error=last_error,
        )


def conversation_lines(event: WakeWordAppEvent, mind_name: str) -> list[str]:
    """Chat lines a UI should append for one runtime event.

    The conversation stream shows only the spoken exchange: the command
    that woke the mind, and what the mind said back.
    """
    lines: list[str] = []
    if event.kind == "dispatching" and event.command_text:
        lines.append(f"You  >  {event.command_text}")
    if event.kind == "response" and event.response_text:
        lines.append(f"{mind_name}  >  {event.response_text}")
    return lines


class WakeWordWindow:
    """Tk window: on/off, mute standby, and a live conversation stream."""

    def __init__(
        self,
        root: tk.Misc,
        controller: WakeWordWindowController | None = None,
        poll_ms: int = 250,
    ) -> None:
        self._root = root
        self._controller = controller or WakeWordWindowController()
        self._poll_ms = poll_ms
        self._mind_name = _display_name()

        self._root.title(f"{self._mind_name} Listener")
        self._root.geometry("720x560")

        self._status_var = tk.StringVar(value="Offline")
        self._last_error_var = tk.StringVar(value="")

        container = tk.Frame(root, padx=18, pady=18, bg="#161b22")
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text=f"{self._mind_name} Wake Word",
            font=("Segoe UI Semibold", 22),
            fg="#f0f6fc",
            bg="#161b22",
        ).pack(anchor="w")
        self._status_label = tk.Label(
            container,
            textvariable=self._status_var,
            font=("Segoe UI", 14),
            fg="#7ee787",
            bg="#161b22",
            pady=10,
        )
        self._status_label.pack(anchor="w")

        button_row = tk.Frame(container, bg="#161b22")
        button_row.pack(anchor="w", pady=(0, 14))
        self._start_button = tk.Button(button_row, text="Start", width=12, command=self._start)
        self._start_button.pack(side="left")
        self._mute_button = tk.Button(button_row, text="Mute", width=12, command=self._toggle_mute)
        self._mute_button.pack(side="left", padx=(10, 0))
        self._stop_button = tk.Button(button_row, text="Stop", width=12, command=self._stop)
        self._stop_button.pack(side="left", padx=(10, 0))

        tk.Label(
            container,
            text="Conversation",
            font=("Segoe UI Semibold", 12),
            fg="#f0f6fc",
            bg="#161b22",
            pady=6,
        ).pack(anchor="w")
        self._conversation = scrolledtext.ScrolledText(
            container,
            height=12,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 11),
            bg="#0d1117",
            fg="#e6edf3",
        )
        self._conversation.pack(fill="both", expand=True)

        tk.Label(
            container,
            text="Activity",
            font=("Segoe UI Semibold", 11),
            fg="#8b949e",
            bg="#161b22",
            pady=6,
        ).pack(anchor="w")
        self._activity = scrolledtext.ScrolledText(
            container,
            height=5,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            bg="#0d1117",
            fg="#8b949e",
        )
        self._activity.pack(fill="x")

        tk.Label(
            container,
            textvariable=self._last_error_var,
            font=("Segoe UI", 10),
            fg="#f85149",
            bg="#161b22",
            wraplength=660,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._render_state(self._controller.state)
        self._start()
        self._poll_events()

    def _start(self) -> None:
        self._controller.start()
        self._render_state(self._controller.state)

    def _stop(self) -> None:
        self._controller.stop()
        self._render_state(self._controller.state)

    def _toggle_mute(self) -> None:
        self._controller.set_muted(not self._controller.muted)
        self._render_state(self._controller.state)

    def _poll_events(self) -> None:
        for event in self._controller.drain_events():
            self._append_activity(event)
            for line in conversation_lines(event, self._mind_name):
                self._append_conversation(line)
        self._render_state(self._controller.state)
        self._root.after(self._poll_ms, self._poll_events)

    def _append_conversation(self, line: str) -> None:
        self._conversation.configure(state="normal")
        self._conversation.insert("end", line + "\n\n")
        self._conversation.see("end")
        self._conversation.configure(state="disabled")

    def _append_activity(self, event: WakeWordAppEvent) -> None:
        line = event.message
        if event.transcript:
            line = f"{line}: {event.transcript}"
        elif event.command_text:
            line = f"{line}: {event.command_text}"
        self._activity.configure(state="normal")
        self._activity.insert("end", line + "\n")
        self._activity.see("end")
        self._activity.configure(state="disabled")

    def _render_state(self, state: WakeWordWindowState) -> None:
        self._status_var.set(state.status_text)
        self._last_error_var.set(state.last_error)
        self._status_label.configure(fg="#d29922" if state.muted else "#7ee787")
        self._start_button.configure(state="disabled" if state.running else "normal")
        self._stop_button.configure(state="normal" if state.running else "disabled")
        self._mute_button.configure(
            state="normal" if state.running else "disabled",
            text="Unmute" if state.muted else "Mute",
        )

    def _on_close(self) -> None:
        self._controller.close()
        self._root.destroy()


def launch_wake_word_window(
    root_factory: Callable[[], tk.Misc] = tk.Tk,
    controller: WakeWordWindowController | None = None,
) -> None:
    """Launch the desktop smart-speaker window."""

    root = root_factory()
    WakeWordWindow(root=root, controller=controller)
    root.mainloop()
