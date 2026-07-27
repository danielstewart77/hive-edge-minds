"""
Hive Mind Telegram Bot.

Thin HTTP client to the gateway server (server.py).
Supports text messages and voice notes (STT/TTS via voice-server).
All Claude Code interaction flows through the gateway — no SDK dependency.
"""

import asyncio
import io
import json
import logging
import os
import sys
import time

import aiohttp
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import config
from bots.bot_utils import get_lock, get_queue, time_ago
from bots.gateway_client import GatewayClient
from bots.skills import get_skills
from hive_logging import configure_logging, log_event

log = configure_logging("hive-mind-telegram")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TELEGRAM_MSG_LIMIT = 4096
# Gateway/sessions/broker → hive-comms (containerised, NS-owned).
COMMS_URL = os.environ.get("COMMS_URL", "http://127.0.0.1:8426")
COMMS_BEARER_TOKEN = os.environ.get("COMMS_BEARER_TOKEN", "")
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://localhost:8422")
# When on (default), every conversational reply is voiced: text still streams,
# a voice note follows. Set ALWAYS_VOICE=0 in .env for text-only except when
# the user sends a voice message. A missing/unreachable voice server degrades
# to text-only (the TTS task logs and drops).
ALWAYS_VOICE = os.environ.get("ALWAYS_VOICE", "1").strip().lower() not in ("0", "false", "no", "")
# Hive-tools — used by /models to list Ollama-served models.
HIVE_TOOLS_URL = os.environ.get("HIVE_TOOLS_URL", "http://127.0.0.1:9421")
HIVE_TOOLS_TOKEN = os.environ.get("HIVE_TOOLS_TOKEN", "")

# Surface-specific system prompt appended when spawning Telegram sessions.
# Telegram renders plain text only; voice output is spoken aloud.
# Instruct Claude to respond conversationally — no code blocks, no markdown,
# no technical formatting. Describe code concepts in plain English instead.
TELEGRAM_SURFACE_PROMPT = (
    "You are responding via Telegram. Your responses will be spoken aloud as voice or read as plain text. "
    "CRITICAL: Do not use any special characters for formatting. No asterisks, no pound signs, no backticks, "
    "no hyphens as bullet points, no underscores for emphasis, no angle brackets, no pipes. "
    "Do not write code of any kind — no code blocks, no inline code, no command snippets. "
    "Do not use numbered or bulleted lists. "
    "Write in plain flowing sentences, like natural speech. "
    "If asked about code or technical topics, describe what it does in plain English "
    "the way you would explain it to someone out loud — no syntax, no examples, just the concept."
)

# Global HTTP session and gateway client (created at startup)
http: aiohttp.ClientSession | None = None
gateway: GatewayClient | None = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _is_allowed_user(user_id: int) -> bool:
    """Fail-closed: empty allowlist = no access."""
    return user_id in config.telegram_allowed_users


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------
async def _stt(ogg_bytes: bytes) -> str:
    """POST OGG audio to voice-server /stt, return transcribed text."""
    form = aiohttp.FormData()
    form.add_field("file", ogg_bytes, filename="audio.ogg", content_type="audio/ogg")
    async with http.post(f"{VOICE_SERVER_URL}/stt", data=form) as resp:
        if resp.status != 200:
            raise RuntimeError(f"STT error {resp.status}: {await resp.text()}")
        return (await resp.json())["text"]


async def _tts(text: str) -> bytes:
    """POST text to voice-server /tts, return OGG audio bytes."""
    voice_id = os.getenv("MIND_NAME", "default")
    async with http.post(f"{VOICE_SERVER_URL}/tts", json={"text": text, "voice_id": voice_id}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"TTS error {resp.status}: {await resp.text()}")
        return await resp.read()


# ---------------------------------------------------------------------------
# JSON detection / sanitization helpers
# ---------------------------------------------------------------------------
def _looks_like_json(text: str) -> bool:
    """Return True if text looks like a raw JSON object or array."""
    stripped = text.strip()
    if not stripped:
        return False
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        parsed = json.loads(stripped)
        # Only consider dicts and lists as "JSON payloads" — not bare
        # strings, numbers, booleans, or null.
        return isinstance(parsed, (dict, list))
    except (json.JSONDecodeError, ValueError):
        return False


def _sanitize_response(text: str) -> str:
    """Replace raw JSON payloads with a human-readable confirmation."""
    if _looks_like_json(text):
        return "Done."
    return text


# ---------------------------------------------------------------------------
# Message chunking (Telegram's 4096-char limit)
# ---------------------------------------------------------------------------
def _chunk_message(text: str) -> list[str]:
    """Split text into <=4096 char chunks."""
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:TELEGRAM_MSG_LIMIT])
        text = text[TELEGRAM_MSG_LIMIT:]
    return chunks


async def _reply_chunked(update: Update, text: str) -> None:
    """Reply with text of any length.

    Command replies used to go straight to ``reply_text``, so one that ran
    past Telegram's limit came back as a BadRequest the error handler logs
    as a transient network blip and drops. The user sees nothing at all —
    the command reads as broken rather than as too chatty.
    """
    for chunk in _chunk_message(text):
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------
async def _stream_to_message(
    sent,
    user_id: int,
    chat_id: int,
    prompt: str,
    edit_interval: float = 2.0,
    images: list[dict] | None = None,
    voice: bool = False,
    chat=None,
) -> list[str]:
    """Stream a gateway response, progressively editing sent as chunks arrive.

    Returns the final list of message chunks.

    When voice=True, the full response is converted to a single voice message
    after streaming completes, so text arrives progressively and voice follows.
    """
    accumulated = ""
    last_edit = 0.0

    async for text_chunk in gateway.query_stream(user_id, chat_id, prompt, images=images):
        # Concatenate without separator. Per-token deltas (when the mind has
        # --include-partial-messages enabled) include their own whitespace;
        # buffered assistant text already has its own paragraph breaks.
        accumulated += text_chunk
        now = time.monotonic()
        if now - last_edit >= edit_interval:
            preview = _chunk_message(accumulated)[0]
            try:
                await sent.edit_text(preview)
            except Exception:
                pass  # MessageNotModified or rate limit — skip this update
            last_edit = now

    if not accumulated:
        # The gateway forwards mind errors as a result event, which arrives
        # via query_stream's result fallback — so an empty stream here means
        # nothing was reported at all.
        accumulated = (
            "ERROR: mind stream ended with no text and no error event. "
            "Check the mind service logs and the hive-comms logs."
        )

    final_chunks = [_sanitize_response(c) for c in _chunk_message(accumulated)]
    try:
        await sent.edit_text(final_chunks[0])
    except Exception:
        pass

    # Send one voice message with the complete response — detached so it
    # doesn't hold the chat lock while TTS round-trips. The text response is
    # already shown above; the voice arrives whenever the TTS service is
    # done. Without this, holding the lock through TTS makes follow-up
    # messages queue up and creates the "response held until next message"
    # n+1 sync glitch.
    if voice and chat:
        full_text = accumulated.strip()
        if full_text:
            async def _send_voice_bg() -> None:
                try:
                    ogg = await _tts(full_text)
                    await chat.send_voice(voice=io.BytesIO(ogg))
                except Exception:
                    log.warning("Final voice TTS/send failed", exc_info=True)
            asyncio.create_task(_send_voice_bg())

    return final_chunks


# ---------------------------------------------------------------------------
# Server command formatters
# ---------------------------------------------------------------------------
def _format_queue_batch(messages: list[str]) -> str:
    """Combine queued messages into one prompt so Claude replies once."""
    if len(messages) == 1:
        return messages[0]
    items = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(messages))
    return (
        "While you were processing my previous message, I sent several more. "
        "Please address all of them in one reply:\n\n" + items
    )


def _format_sessions(sessions: list[dict]) -> str:
    """The session picker, including sessions another surface is holding.

    A conversation started in the browser terminal is listed here with the
    surface it currently lives on, because switching to it moves it: the
    terminal's process ends and the conversation carries on in this chat
    with its whole history intact. That's the point \u2014 you started it at the
    desk and you're not at the desk any more.
    """
    if not sessions:
        return "No sessions found."
    lines = ["Your Sessions:\n"]
    for i, s in enumerate(sessions, 1):
        status_icon = {"running": "\U0001f7e2", "idle": "\U0001f4a4", "closed": "\U0001f534"}.get(
            s["status"], "\u2753"
        )
        autopilot = " \U0001f916" if s.get("autopilot") else ""
        short_id = s["id"][:8]
        summary = s.get("summary", "Untitled")
        last = s.get("last_active", 0)
        ago = time_ago(last) if last else "?"
        where = f" \u2014 on {s.get('surface') or 'another surface'}" if s.get("adoptable") else ""
        lines.append(
            f"{i}. {status_icon}{autopilot} {short_id} \u2014 \"{summary}\" "
            f"[{s.get('model', '?')}] ({ago}){where}"
        )
    if any(s.get("adoptable") for s in sessions):
        lines.append("\nSessions marked with a surface are running elsewhere \u2014 "
                     "/switch moves one here.")
    lines.append("\n/switch <number> \u00b7 /new to start \u00b7 /kill <number> to kill")
    return "\n".join(lines)


def _format_status(data: dict) -> str:
    return (
        f"Server port: {data.get('server_port')}\n"
        f"Default model: {data.get('default_model')}\n"
        f"Sessions: {data.get('running_sessions')}/{data.get('total_sessions')} running"
    )


# ---------------------------------------------------------------------------
# Server command dispatcher
# ---------------------------------------------------------------------------
SERVER_COMMANDS = {"/clear", "/model", "/autopilot", "/kill", "/prune", "/status", "/sessions", "/switch", "/new", "/remember"}


async def _handle_server_command(content: str, user_id: int, chat_id: int) -> str:
    parts = content.split()
    cmd = parts[0]
    log_event(
        log, "surface.command.received", surface="telegram", command=cmd,
        user_id=user_id, client_ref=chat_id,
    )
    result = await gateway.server_command(user_id, chat_id, content)

    if "error" in result:
        log_event(
            log, "surface.command.failed", level=logging.WARNING, surface="telegram",
            command=cmd, user_id=user_id, client_ref=chat_id,
        )
        return f"Error: {result['error']}"
    log_event(
        log, "surface.command.completed", surface="telegram", command=cmd,
        user_id=user_id, client_ref=chat_id,
    )

    if cmd == "/sessions":
        return _format_sessions(result)
    if cmd == "/status":
        return _format_status(result)
    if cmd == "/new":
        return f"New session: {result.get('id', '?')[:8]}"
    if cmd == "/clear":
        return f"Session cleared. New: {result.get('id', '?')[:8]}"
    if cmd == "/model":
        if isinstance(result, list):
            lines = ["Available models:"]
            for m in result:
                lines.append(f"- {m['name']} ({m['provider']})")
            lines.append("\n/model <name> to switch")
            return "\n".join(lines)
        msg = f"Switched to {result.get('model')}"
        if result.get("warning"):
            msg += f"\n\u26a0\ufe0f {result['warning']}"
        return msg
    if cmd == "/autopilot":
        on = result.get("autopilot", False)
        summary = result.get("summary", "this session")
        if on:
            return f"\U0001f916 Autopilot ON for \"{summary}\""
        return f"\U0001f512 Autopilot OFF for \"{summary}\""
    if cmd == "/switch":
        return f"Resumed \"{result.get('summary', '?')}\""
    if cmd == "/kill":
        return f"Killed \"{result.get('summary', '?')}\" (status: {result.get('status')})"
    if cmd == "/prune":
        killed = result.get("killed") or []
        kept = result.get("kept")
        if not killed:
            return "Nothing to prune — only the active session exists."
        kept_str = f" Kept active: {kept[:8]}." if kept else ""
        return f"Pruned {len(killed)} session(s).{kept_str}"

    return "Done."


# ---------------------------------------------------------------------------
# Auth guard helper
# ---------------------------------------------------------------------------
async def _auth_check(update: Update) -> bool:
    if not _is_allowed_user(update.effective_user.id):
        log_event(
            log, "surface.auth.rejected", level=logging.WARNING, surface="telegram",
            user_id=update.effective_user.id, client_ref=update.effective_chat.id,
        )
        await update.message.reply_text("Not authorized.")
        return False
    return True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/sessions", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/new", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/remember", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/clear", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/status", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    name = " ".join(context.args) if context.args else None
    cmd = f"/model {name}" if name else "/model"
    msg = await _handle_server_command(cmd, update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List models available to this mind: static aliases from config.yaml
    plus the live Ollama list from hive-tools."""
    if not await _auth_check(update):
        return

    lines: list[str] = ["*Available models*"]

    static = sorted(config.models.keys())
    if static:
        lines.append("")
        lines.append("*Claude (via Anthropic API):*")
        for alias in static:
            marker = "  ← default" if alias == config.default_model else ""
            lines.append(f"  • `{alias}`{marker}")

    ollama_configured = "ollama" in config.providers
    if not ollama_configured:
        lines.append("")
        lines.append("_(No Ollama provider configured for this mind.)_")
    else:
        try:
            headers = {"Authorization": f"Bearer {HIVE_TOOLS_TOKEN}"} if HIVE_TOOLS_TOKEN else {}
            async with http.get(
                f"{HIVE_TOOLS_URL}/ollama/models",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    lines.append("")
                    lines.append(f"_Ollama list unavailable (HTTP {resp.status}): {body[:120]}_")
                else:
                    data = await resp.json()
        except Exception as exc:
            lines.append("")
            lines.append(f"_Ollama list unavailable: {exc}_")
            data = None

        if data:
            base = data.get("ollama_base_url", "?")
            default = data.get("default_model", "")
            models_list = data.get("models", [])
            lines.append("")
            lines.append(f"*Ollama (`{base}`):*")
            if not models_list:
                lines.append("  _(no models pulled yet)_")
            else:
                for m in models_list:
                    name = m.get("name", "?")
                    psize = m.get("parameter_size") or ""
                    quant = m.get("quantization") or ""
                    family = m.get("family") or ""
                    bits = " · ".join(b for b in (family, psize, quant) if b)
                    marker = "  ← default" if name == default else ""
                    suffix = f" — {bits}" if bits else ""
                    lines.append(f"  • `{name}`{suffix}{marker}")

    lines.append("")
    lines.append("_Switch with_ `/model <name>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/autopilot", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /switch <number>")
        return
    msg = await _handle_server_command(f"/switch {target}", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /kill <number>")
        return
    msg = await _handle_server_command(f"/kill {target}", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_prune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    msg = await _handle_server_command("/prune", update.effective_user.id, update.effective_chat.id)
    await _reply_chunked(update, msg)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interrupt the running command without killing the session.

    Bypasses the message queue entirely — does NOT acquire the chat lock.
    """
    if not await _auth_check(update):
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session_id = await gateway.find_active_session(user_id, chat_id)

    if session_id is None:
        await update.message.reply_text("No active session.")
        return

    result = await gateway.interrupt_session(session_id)

    if result.get("message") == "nothing_running":
        await update.message.reply_text("Nothing running.")
    else:
        await update.message.reply_text("Interrupted.")


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    skills = get_skills()
    if not skills:
        await update.message.reply_text("No skills found.")
        return
    lines = ["Available Skills\n"]
    for s in skills:
        hint = f" {s['argument_hint']}" if s["argument_hint"] else ""
        lines.append(f"\u2022 {s['name']}{hint} \u2014 {s['description']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_skill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _auth_check(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /skill <name> [args]")
        return
    name = context.args[0]
    args = " ".join(context.args[1:]) if len(context.args) > 1 else None
    prompt = f"/{name} {args}" if args else f"/{name}"

    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    async with lock:
        sent = await update.message.reply_text("\u2026")
        final_chunks = await _stream_to_message(
            sent, update.effective_user.id, chat_id, prompt,
            voice=ALWAYS_VOICE, chat=update.effective_chat,
        )
        for extra in final_chunks[1:]:
            await update.effective_chat.send_message(extra)


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        log_event(
            log, "surface.auth.rejected", level=logging.WARNING, surface="telegram",
            user_id=update.effective_user.id, client_ref=update.effective_chat.id,
        )
        return

    # In group chats, only respond to @mentions
    if update.effective_chat.type != "private":
        bot_username = context.bot.username
        if not (update.message.text and f"@{bot_username}" in update.message.text):
            return

    content = update.message.text or ""
    if update.effective_chat.type != "private" and context.bot.username:
        content = content.replace(f"@{context.bot.username}", "").strip()

    if not content:
        return

    chat_id = update.effective_chat.id
    log_event(
        log, "surface.message.received", surface="telegram", message_type="text",
        user_id=update.effective_user.id, client_ref=chat_id, content_chars=len(content),
    )
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(content)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, content,
                voice=ALWAYS_VOICE, chat=update.effective_chat,
            )
            for extra in final_chunks[1:]:
                await update.effective_chat.send_message(extra)
        except Exception:
            log.exception("Error processing message in chat %s", chat_id)
            err = f"⚠ {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            await update.message.reply_text(err)


        # Drain queue in a loop — new messages may arrive during batch processing
        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                sent2 = await update.effective_chat.send_message("\u2026")
                final_chunks2 = await _stream_to_message(
                    sent2, update.effective_user.id, chat_id, batch,
                    voice=ALWAYS_VOICE, chat=update.effective_chat,
                )
                for extra in final_chunks2[1:]:
                    await update.effective_chat.send_message(extra)
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                err = f"⚠ queued-batch {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
                await update.effective_chat.send_message(err)


# ---------------------------------------------------------------------------
# Photo message handler
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    # In group chats, only respond to @mentions in the caption
    if update.effective_chat.type != "private":
        bot_username = context.bot.username
        caption = update.message.caption or ""
        if f"@{bot_username}" not in caption:
            return
        caption = caption.replace(f"@{context.bot.username}", "").strip()
    else:
        caption = update.message.caption or ""

    content = caption if caption else "Please analyze this image."

    chat_id = update.effective_chat.id
    log_event(
        log, "surface.message.received", surface="telegram", message_type="photo",
        user_id=update.effective_user.id, client_ref=chat_id, caption_chars=len(content),
    )
    lock = get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Still processing your previous message — yours is queued and will follow.")

    async with lock:
        try:
            import base64

            # Download highest resolution photo
            photo = update.message.photo[-1]
            file = await photo.get_file()
            photo_bytes = bytes(await file.download_as_bytearray())
            b64_data = base64.b64encode(photo_bytes).decode("ascii")

            # Persist to a dated drop folder so photos survive the turn. The
            # mind can move each file into the right house's images subfolder
            # via Bash. Without this, photos only existed as a base64 blob in
            # this turn's context and were lost forever after the turn closed.
            from datetime import datetime
            from pathlib import Path
            repo_root = Path(__file__).resolve().parents[1]
            drop_dir = repo_root / "data" / "telegram_photos" / datetime.now().strftime("%Y-%m-%d")
            drop_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S_%f")
            saved_path = drop_dir / f"{ts}_{chat_id}_{photo.file_unique_id}.jpg"
            saved_path.write_bytes(photo_bytes)
            relative = saved_path.relative_to(repo_root)
            log_event(
                log, "surface.photo.saved", surface="telegram", user_id=update.effective_user.id,
                client_ref=chat_id, path=str(relative),
                bytes=len(photo_bytes),
            )

            images = [{"media_type": "image/jpeg", "data": b64_data}]

            # Tell the mind where the file lives so it can be moved into the
            # correct house's images folder in the same turn.
            content_with_path = (
                f"{content}\n\n[saved to {relative} \u2014 move into the active house's images folder if appropriate]"
            )

            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, content_with_path, images=images,
                voice=ALWAYS_VOICE, chat=update.effective_chat,
            )
            for extra in final_chunks[1:]:
                await update.effective_chat.send_message(extra)
        except Exception:
            log.exception("Error processing photo in chat %s", chat_id)
            await update.message.reply_text(
                f"⚠ image {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            )



# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed_user(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    log_event(
        log, "surface.message.received", surface="telegram", message_type="voice",
        user_id=update.effective_user.id, client_ref=chat_id,
    )
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    # STT happens outside the lock so we have text to queue if busy.
    # Telegram's getFile/download occasionally stalls; retry once on transient
    # network errors so a single blip doesn't surface to the user.
    async def _fetch_voice_bytes() -> bytes:
        voice_file = await update.message.voice.get_file()
        return bytes(await voice_file.download_as_bytearray())

    try:
        try:
            ogg_bytes = await _fetch_voice_bytes()
        except (TimedOut, NetworkError) as exc:
            log.warning("voice fetch transient failure (%s) — retrying once", exc.__class__.__name__)
            await asyncio.sleep(1.0)
            ogg_bytes = await _fetch_voice_bytes()
        text = await _stt(ogg_bytes)
    except Exception:
        log.exception("STT failed in chat %s", chat_id)
        await update.message.reply_text("Couldn't transcribe your audio.")
        return

    log.info("STT: %r", text[:80])
    if not text.strip():
        await update.message.reply_text("Couldn't transcribe audio.")
        return

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(text)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, text,
                voice=True, chat=update.effective_chat,
            )
            for extra in final_chunks[1:]:
                await update.effective_chat.send_message(extra)
        except Exception:
            log.exception("Unexpected error in voice handler for chat %s", chat_id)
            err = f"⚠ voice {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            await update.message.reply_text(err)


        # Drain queue in a loop — new messages may arrive during batch processing
        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                sent2 = await update.effective_chat.send_message("\u2026")
                final_chunks2 = await _stream_to_message(
                    sent2, update.effective_user.id, chat_id, batch,
                    voice=ALWAYS_VOICE, chat=update.effective_chat,
                )
                for extra in final_chunks2[1:]:
                    await update.effective_chat.send_message(extra)
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                err = f"⚠ queued-batch {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
                await update.effective_chat.send_message(err)


# ---------------------------------------------------------------------------
# Catch-all for unregistered slash commands
# ---------------------------------------------------------------------------
async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route unregistered slash commands as prompts to the gateway.

    Any /command that is not handled by a registered CommandHandler falls
    through to this catch-all.  The full command text (including the /)
    is sent as a regular prompt so Claude can process it as a skill.
    """
    if not _is_allowed_user(update.effective_user.id):
        await update.message.reply_text("Not authorized.")
        return

    content = update.message.text or ""

    # Strip @botname suffix in group chats (e.g. /remember@botname → /remember)
    if context.bot.username:
        content = content.replace(f"@{context.bot.username}", "")
    content = content.strip()

    if not content:
        return

    chat_id = update.effective_chat.id
    lock = get_lock(chat_id)
    queue = get_queue(chat_id)

    if lock.locked():
        pos = queue.qsize() + 1
        await queue.put(content)
        await update.message.reply_text(f"Still processing — yours is queued (#{pos}).")
        return

    async with lock:
        try:
            sent = await update.message.reply_text("\u2026")
            final_chunks = await _stream_to_message(
                sent, update.effective_user.id, chat_id, content,
                voice=ALWAYS_VOICE, chat=update.effective_chat,
            )
            for extra in final_chunks[1:]:
                await update.effective_chat.send_message(extra)
        except Exception:
            log.exception("Error processing unknown command in chat %s", chat_id)
            err = f"⚠ {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
            await update.message.reply_text(err)


        # Drain queue in a loop — new messages may arrive during batch processing
        while not queue.empty():
            queued: list[str] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            batch = _format_queue_batch(queued)
            try:
                sent2 = await update.effective_chat.send_message("\u2026")
                final_chunks2 = await _stream_to_message(
                    sent2, update.effective_user.id, chat_id, batch,
                    voice=ALWAYS_VOICE, chat=update.effective_chat,
                )
                for extra in final_chunks2[1:]:
                    await update.effective_chat.send_message(extra)
            except Exception:
                log.exception("Error processing queued batch in chat %s", chat_id)
                err = f"⚠ queued-batch {type(_exc:=sys.exc_info()[1]).__name__}: {_exc}"[:3500]
                await update.effective_chat.send_message(err)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _get_bot_token() -> str:
    """Load Telegram bot token — env first, keyring fallback (F10).

    Precedence flipped 2026-05-05: previously keyring-first, env-fallback.
    Env-first means a `.env`-provided token wins, with keyring kept as
    a transitional fallback during F10 migration. Once every deployment
    has the token in `.env`, the keyring branch can be removed.

    TELEGRAM_BOT_TOKEN_KEYRING_KEY still overrides the keyring key lookup
    when the fallback is exercised, allowing multiple bot instances to
    run from the same image with different tokens.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    log.error("TELEGRAM_BOT_TOKEN not found in environment.")
    sys.exit(1)


async def _on_startup(app) -> None:
    global http, gateway
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0, sock_read=0))
    # Namespace the session client_type so two telegram bots with the same
    # authorized user don't share a session. MIND_ID is canonical — same
    # identity key used in broker.minds, sessions.mind_id, and lucent
    # writes. Don't drift back to MIND_NAME here; it creates a parallel
    # naming system for the same concept (see scripts/migrations/
    # 2026-05-15-surface-namespace-uuid.py for the cleanup that fixed it).
    mind_id = os.environ["MIND_ID"]
    surface_name = f"telegram:{mind_id}"
    gateway = GatewayClient(
        http, COMMS_URL, surface_name,
        surface_prompt=TELEGRAM_SURFACE_PROMPT,
        mind_id=mind_id,
        bearer_token=COMMS_BEARER_TOKEN or None,
    )
    log.info(
        "Hive Mind Telegram bot started (gateway=%s, voice=%s)",
        COMMS_URL,
        VOICE_SERVER_URL,
    )
    log.info("Allowed users: %s", config.telegram_allowed_users)


async def _on_shutdown(app) -> None:
    if http:
        await http.close()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler. Swallow transient PTB network errors at info level;
    log everything else with traceback so the bot stays alive."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.info("transient telegram network error (%s): %s", err.__class__.__name__, err)
        return
    log.exception("unhandled exception in telegram handler", exc_info=err)


def _build_application(token: str):
    """Build the Telegram Application with all handlers wired up."""
    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)
        .connect_timeout(10.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(5.0)
        .get_updates_read_timeout(30.0)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("prune", cmd_prune))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("skill", cmd_skill))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    # Catch-all: any /command not matched above is routed as a prompt
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    app.add_error_handler(_error_handler)

    return app


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Swallow transient polling errors; log everything else."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.info("Transient telegram network error: %s", err)
        return
    log.exception("Unhandled telegram error", exc_info=err)


async def _proactive_consumer(app) -> None:
    """Drain the proactive queue and post unsolicited turns to Telegram.

    Runs for the lifetime of the bot. Each ``(chat_id, text)`` item is an
    assistant turn produced without an inbound user message (background agent
    completion, scheduled wakeup, rotation notice). Splits messages over
    Telegram's 4096-char limit; a failed send never kills the loop.
    """
    from bots import proactive

    while True:
        chat_id, text = await proactive.get()
        try:
            for chunk in _chunk_message(text):
                await app.bot.send_message(chat_id=chat_id, text=chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Proactive delivery to chat %s failed", chat_id)


async def run_telegram_bot() -> None:
    """Async entry point — embeds the bot inside a larger asyncio program.

    Initialises the Application, starts long-polling, blocks until cancelled
    (e.g. on SIGTERM), then cleans up.
    """
    import asyncio as _asyncio

    token = _get_bot_token()
    app = _build_application(token)

    await app.initialize()
    # post_init/post_shutdown hooks only fire under run_polling/run_webhook —
    # invoke them explicitly when embedding the app in a custom event loop.
    await _on_startup(app)
    await app.start()
    await app.updater.start_polling()

    # Background consumer for proactive (unsolicited) assistant turns pushed
    # by mind_server via bots.proactive.
    proactive_task = _asyncio.ensure_future(_proactive_consumer(app))

    try:
        await _asyncio.Event().wait()
    except _asyncio.CancelledError:
        pass
    finally:
        proactive_task.cancel()
        try:
            await proactive_task
        except _asyncio.CancelledError:
            pass
        await app.updater.stop()
        await app.stop()
        await _on_shutdown(app)
        await app.shutdown()


if __name__ == "__main__":
    token = _get_bot_token()
    app = _build_application(token)
    app.run_polling()
