"""Tests for the ALWAYS_VOICE behavior in telegram_bot.py.

When ALWAYS_VOICE is on (the default), every conversational reply the mind
streams is also voiced: the text streams progressively and a voice note
follows. These are source-inspection tests (matching the repo's other
telegram_bot tests) so they need no telegram/torch deps to run.
"""
import ast
from pathlib import Path


def _get_source() -> str:
    return (Path(__file__).resolve().parents[2] / "bots" / "telegram_bot.py").read_text(encoding="utf-8")


def test_always_voice_flag_defined_and_defaults_on():
    """ALWAYS_VOICE is read from env and defaults to on."""
    source = _get_source()
    assert "ALWAYS_VOICE" in source, "ALWAYS_VOICE flag missing"
    assert 'os.environ.get("ALWAYS_VOICE", "1")' in source, (
        "ALWAYS_VOICE must default to on (\"1\")"
    )


def test_conversational_handlers_voice_their_replies():
    """Every conversational _stream_to_message call passes voice=ALWAYS_VOICE.

    The only hard-coded voice=True is the voice-message handler (you spoke, you
    always get voice back); every other streamed reply is gated on the flag.
    """
    source = _get_source()
    # handle_text (+drain), handle_unknown_command (+drain), handle_voice drain,
    # cmd_skill, handle_photo = 7 flag-gated voice sites.
    assert source.count("voice=ALWAYS_VOICE") >= 7, (
        f"Expected >=7 voice=ALWAYS_VOICE call sites, found "
        f"{source.count('voice=ALWAYS_VOICE')}"
    )
    # The voice-input handler keeps its unconditional voice reply.
    assert "voice=True, chat=update.effective_chat," in source


def test_no_unvoiced_stream_calls_remain():
    """No _stream_to_message call streams a reply without a voice flag.

    Parse the AST and assert every call to _stream_to_message carries either a
    ``voice`` keyword (flag-gated or True). Guards against a new handler being
    added later that silently goes back to text-only.
    """
    source = _get_source()
    tree = ast.parse(source)
    unvoiced = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "_stream_to_message":
            continue
        # Skip the function definition's own signature (handled separately).
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "voice" not in kwargs:
            unvoiced.append(ast.dump(node))
    assert not unvoiced, f"Found _stream_to_message calls with no voice flag: {unvoiced}"


def test_syntax_valid():
    ast.parse(_get_source())
