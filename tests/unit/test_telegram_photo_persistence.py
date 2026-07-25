"""Tests verifying handle_photo persists incoming photos to a dated drop folder.

Without disk persistence the Telegram bot only forwarded the base64 blob into
the current turn, so photos vanished as soon as the turn closed. The fix
writes each photo to `data/telegram_photos/YYYY-MM-DD/` and tells the mind
where the file landed so it can be moved into the right house's images
subfolder.
"""
import ast
from pathlib import Path


def _source() -> str:
    return (Path(__file__).resolve().parents[2] / "bots" / "telegram_bot.py").read_text()


def _handle_photo_source() -> str:
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_photo":
            return ast.unparse(node)
    raise AssertionError("handle_photo function not found in telegram_bot.py")


def test_handle_photo_writes_to_dated_drop_folder():
    src = _handle_photo_source()
    assert "data" in src and "telegram_photos" in src, (
        "handle_photo must write under data/telegram_photos/"
    )
    assert "%Y-%m-%d" in src, "dated drop folder must use a YYYY-MM-DD subdir"
    assert "write_bytes" in src, "photo bytes must be written to disk"


def test_handle_photo_passes_saved_path_to_mind():
    src = _handle_photo_source()
    assert "relative" in src and "saved to" in src, (
        "the saved path must be appended to the content sent to the mind so it "
        "can move the file into the active house's images folder"
    )


def test_handle_photo_still_streams_base64():
    src = _handle_photo_source()
    assert "b64encode" in src and "images=images" in src, (
        "the inline base64 image must still be forwarded to the mind for the "
        "current turn so visual analysis still works"
    )
