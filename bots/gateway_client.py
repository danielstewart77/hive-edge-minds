"""HTTP client for the hive-comms gateway.

Used by ``bots/telegram_bot.py``. Skill discovery (``bots/skills.py``)
and per-chat asyncio primitives (``bots/bot_utils.py``) used to live
here and were split out in Phase B7.
"""

import json
import logging
import time
from typing import Any, AsyncGenerator

import aiohttp

from hive_logging import log_event, log_if_slow

log = logging.getLogger("hive-mind.gateway-client")


class GatewayClient:
    """HTTP client for the Hive Mind gateway server."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        server_url: str,
        owner_type: str,
        surface_prompt: str | None = None,
        *,
        mind_id: str,
        bearer_token: str | None = None,
    ):
        self.http = http
        self.server_url = server_url
        self.owner_type = owner_type
        self.surface_prompt = surface_prompt
        self.mind_id = mind_id
        self._bearer_token = bearer_token

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}

    async def find_active_session(
        self, user_id: int, client_ref: int | str
    ) -> str | None:
        """Look up an active session for this client. Returns the session ID
        if one exists, or ``None`` if there is no active session.

        Unlike :meth:`ensure_session`, this never creates a new session.
        """
        async with self.http.get(
            f"{self.server_url}/sessions",
            params={"client_type": self.owner_type, "client_ref": str(client_ref)},
            headers=self._auth_headers,
        ) as resp:
            data = await resp.json()
            for s in data:
                if s.get("is_active"):
                    return s["id"]
        return None

    async def ensure_session(self, user_id: int, client_ref: int | str) -> str:
        """Get active session for this client, or create one."""
        async with self.http.get(
            f"{self.server_url}/sessions",
            params={"client_type": self.owner_type, "client_ref": str(client_ref)},
            headers=self._auth_headers,
        ) as resp:
            data = await resp.json()
            for s in data:
                if s.get("is_active"):
                    log_event(
                        log, "gateway.session.reused", level=logging.DEBUG,
                        session_id=s["id"], mind_id=self.mind_id,
                        owner_type=self.owner_type, client_ref=str(client_ref),
                    )
                    return s["id"]

        payload: dict = {
            "owner_type": self.owner_type,
            "owner_ref": str(user_id),
            "client_ref": str(client_ref),
            "mind_id": self.mind_id,
        }
        if self.surface_prompt:
            payload["surface_prompt"] = self.surface_prompt
        async with self.http.post(
            f"{self.server_url}/sessions", json=payload, headers=self._auth_headers,
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                log_event(
                    log, "gateway.session.create.failed", level=logging.ERROR,
                    status_code=resp.status, mind_id=self.mind_id,
                    owner_type=self.owner_type, client_ref=str(client_ref),
                )
                raise RuntimeError(f"Gateway session creation failed: HTTP {resp.status}")
            session_id = data["id"]
            log_event(
                log, "gateway.session.created", session_id=session_id,
                mind_id=self.mind_id, owner_type=self.owner_type,
                client_ref=str(client_ref), user_id=user_id,
            )
            return session_id

    async def server_command(
        self, user_id: int, client_ref: int | str, content: str
    ) -> dict:
        """Send a server command and return the JSON response."""
        command = content.split(maxsplit=1)[0] if content else ""
        started = time.monotonic()
        async with self.http.post(
            f"{self.server_url}/command",
            json={
                "content": content,
                "owner_type": self.owner_type,
                "owner_ref": str(user_id),
                "client_ref": str(client_ref),
                "mind_id": self.mind_id,
            },
            headers=self._auth_headers,
        ) as resp:
            result = await resp.json()
            log_event(
                log, "gateway.command.completed" if resp.status < 400 else "gateway.command.failed",
                level=logging.INFO if resp.status < 400 else logging.ERROR,
                command=command, status_code=resp.status, mind_id=self.mind_id,
                owner_type=self.owner_type, client_ref=str(client_ref), user_id=user_id,
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            return result

    async def interrupt_session(self, session_id: str) -> dict:
        """Send an interrupt request for a session. Returns the JSON response."""
        async with self.http.post(
            f"{self.server_url}/sessions/{session_id}/interrupt",
            headers=self._auth_headers,
        ) as resp:
            result = await resp.json()
            # Several lightweight/test transports omit a concrete status;
            # preserving the old return-only behavior is safer than making
            # observability a new failure mode.
            status = resp.status if isinstance(resp.status, int) else 200
            log_event(
                log, "gateway.session.interrupt.completed" if status < 400 else "gateway.session.interrupt.failed",
                level=logging.INFO if status < 400 else logging.ERROR,
                session_id=session_id, mind_id=self.mind_id, status_code=status,
            )
            return result

    async def query_stream(
        self, user_id: int, client_ref: int | str, prompt: str,
        images: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Yield assistant text chunks from the gateway SSE response as they arrive.

        Yields each assistant message block as it comes in, enabling callers
        to update a live message progressively rather than waiting for the full
        response.  Falls back to the result event text if no assistant blocks
        were received (e.g. tool-only turns).
        """
        session_id = await self.ensure_session(user_id, client_ref)
        started = time.monotonic()
        log_event(
            log, "gateway.turn.started", session_id=session_id, mind_id=self.mind_id,
            owner_type=self.owner_type, client_ref=str(client_ref), user_id=user_id,
            content_chars=len(prompt), image_count=len(images or []),
        )
        yielded_any = False
        result_fallback = ""

        # SSE streams can be very long-lived (docker builds, long
        # tool calls, etc.), so override the default aiohttp timeouts.
        sse_timeout = aiohttp.ClientTimeout(total=0, sock_read=0)
        payload: dict[str, Any] = {"content": prompt}
        if images:
            payload["images"] = images
        async with self.http.post(
            f"{self.server_url}/sessions/{session_id}/message",
            json=payload,
            timeout=sse_timeout,
            headers=self._auth_headers,
        ) as resp:
            if resp.status != 200:
                error_text = ""
                try:
                    data = await resp.json()
                    if isinstance(data, dict):
                        error_text = str(data.get("error", ""))
                    else:
                        error_text = str(data)
                except Exception:
                    error_text = await resp.text()
                error_text = error_text or f"HTTP {resp.status}"
                log_event(
                    log, "gateway.turn.failed", level=logging.ERROR,
                    session_id=session_id, mind_id=self.mind_id, status_code=resp.status,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise RuntimeError(
                    f"Gateway message request failed for session {session_id}: {error_text}"
                )
            buf = ""
            # When a mind spawns claude with --include-partial-messages, we
            # receive stream_event events containing per-token text_delta
            # payloads in addition to the buffered `assistant` event at the
            # end of each content block. Prefer the deltas when present and
            # suppress the buffered text to avoid duplication.
            saw_partial_text = False
            async for chunk in resp.content.iter_any():
                buf += chunk.decode()
                while "\n" in buf:
                    raw_line, buf = buf.split("\n", 1)
                    raw_line = raw_line.strip()
                    if not raw_line or not raw_line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(raw_line.removeprefix("data: "))
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "stream_event":
                        # Anthropic-shaped partial event. We care about
                        # text_delta payloads inside content_block_delta.
                        inner = event.get("event", {})
                        if inner.get("type") == "content_block_delta":
                            delta = inner.get("delta", {})
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                yield delta["text"]
                                yielded_any = True
                                saw_partial_text = True
                    elif etype == "assistant":
                        if saw_partial_text:
                            # Already streamed via deltas; skip the buffered
                            # text to avoid duplication.
                            continue
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text" and block.get("text"):
                                yield block["text"]
                                yielded_any = True
                    elif etype == "result":
                        result_fallback = event.get("result", "")

        if not yielded_any and result_fallback:
            yield result_fallback
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        log_event(
            log, "gateway.turn.completed", session_id=session_id, mind_id=self.mind_id,
            elapsed_ms=elapsed_ms,
            yielded_text=yielded_any, used_result_fallback=bool(not yielded_any and result_fallback),
        )
        log_if_slow(
            log, "gateway.turn.slow", elapsed_ms, session_id=session_id,
            mind_id=self.mind_id,
        )

    async def query(self, user_id: int, client_ref: int | str, prompt: str,
                    images: list[dict] | None = None) -> str:
        """Send a query and return the complete response text (non-streaming)."""
        texts: list[str] = []
        async for text in self.query_stream(user_id, client_ref, prompt, images=images):
            texts.append(text)
        combined = "\n\n".join(texts)
        if not combined:
            raise RuntimeError(
                f"Empty response from gateway for user={user_id} client_ref={client_ref}: "
                "stream produced no text and no result fallback."
            )
        return combined
