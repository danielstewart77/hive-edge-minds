"""Resize plumbing for the web-terminal attach.

The attach WS protocol: BINARY frames are raw terminal bytes, TEXT frames
are JSON control messages. A resize frame retargets the pty the tmux client
runs in; tmux answers the SIGWINCH by resizing the pane and repainting it
from its own screen model, which is what keeps a rotated phone from holding
a desktop's layout. Everything below is about getting the geometry onto that
pty — what the repaint then looks like is tmux's job, covered in
test_tmux_terminal.py.
"""

import fcntl
import os
import pty
import struct
import termios

import tempfile

# mind_server exits at import without its identity env and creates
# CLAUDE_CONFIG_DIR at import time; supply test values before importing.
os.environ.setdefault("MIND_ID", "test-mind-id")
os.environ.setdefault("MIND_NAME", "example")
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="pty-resize-test-"))

import asyncio  # noqa: E402

from mind_server import (  # noqa: E402
    _PtyHandle,
    _clamp_winsize,
    _pty_control_frame,
    _set_pty_winsize,
)


def _get_winsize(fd: int) -> tuple[int, int]:
    rows, cols, _, _ = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    )
    return cols, rows


def _handle(master_fd: int | None, cols: int = 80, rows: int = 24) -> _PtyHandle:
    """A handle around a bare pty — no subprocess needed to test geometry."""
    handle = _PtyHandle("sid", tmux_name="example-sid", claude_sid="conv",
                        cols=cols, rows=rows)
    handle.master_fd = master_fd
    return handle


def test_clamp_winsize_bounds():
    assert _clamp_winsize(120, 32) == (120, 32)
    assert _clamp_winsize(1, 1) == (20, 5)
    assert _clamp_winsize(10_000, 10_000) == (500, 200)


def test_set_pty_winsize_applies_to_pty():
    master_fd, slave_fd = pty.openpty()
    try:
        _set_pty_winsize(master_fd, 132, 43)
        assert _get_winsize(slave_fd) == (132, 43)
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_control_frame_resize_applies_and_claims_frame():
    master_fd, slave_fd = pty.openpty()
    try:
        claimed = _pty_control_frame(_handle(master_fd), '{"type":"resize","cols":100,"rows":30}')
        assert claimed is True
        assert _get_winsize(slave_fd) == (100, 30)
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_control_frame_rejects_plain_text_input():
    # Non-JSON text is terminal input from a legacy client, not control —
    # the caller must write it to the pty.
    master_fd, slave_fd = pty.openpty()
    try:
        assert _pty_control_frame(_handle(master_fd), "ls -la\n") is False
        assert _pty_control_frame(_handle(master_fd), "{not json") is False
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_control_frame_rejects_non_resize_json():
    master_fd, slave_fd = pty.openpty()
    try:
        assert _pty_control_frame(_handle(master_fd), '{"type":"ping"}') is False
        assert _pty_control_frame(_handle(master_fd), '"just a string"') is False
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_control_frame_swallows_malformed_resize():
    # A resize frame with garbage values is claimed (it IS a control frame)
    # but must never crash the pump or write into the byte stream.
    master_fd, slave_fd = pty.openpty()
    try:
        _set_pty_winsize(master_fd, 90, 25)
        handle = _handle(master_fd, cols=90, rows=25)
        assert _pty_control_frame(handle, '{"type":"resize","cols":"wide"}') is True
        assert _get_winsize(slave_fd) == (90, 25)  # unchanged
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_resize_records_the_geometry_and_retargets_the_pty():
    master_fd, slave_fd = pty.openpty()
    try:
        handle = _handle(master_fd, cols=80, rows=24)

        handle.resize(44, 27)

        assert (handle.cols, handle.rows) == (44, 27)
        assert _get_winsize(slave_fd) == (44, 27)
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_resize_clamps_before_applying():
    master_fd, slave_fd = pty.openpty()
    try:
        handle = _handle(master_fd, cols=80, rows=24)

        handle.resize(1, 1)

        assert (handle.cols, handle.rows) == (20, 5)
        assert _get_winsize(slave_fd) == (20, 5)
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_resize_between_attachments_is_recorded_not_dropped():
    # A detached handle has no client and no fd. The geometry still has to
    # stick, because it is what the next client will be spawned at.
    handle = _handle(None, cols=80, rows=24)

    handle.resize(44, 27)

    assert (handle.cols, handle.rows) == (44, 27)


def test_push_reaches_the_attached_socket():
    handle = _handle(None)
    handle.queue = asyncio.Queue()

    handle.push(b"live bytes")

    assert handle.queue.get_nowait() == b"live bytes"


def test_push_with_nobody_attached_is_a_no_op():
    handle = _handle(None)
    handle.push(b"into the void")  # must not raise
