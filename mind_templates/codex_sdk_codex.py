# UNTESTED — scaffold only. Validate before production use.
"""Codex SDK + Codex models template.

Not yet tested. Codex does not currently have a Python SDK equivalent
to claude_code_sdk. This scaffold assumes a future SDK with a similar
query() async generator interface. Do not use until a Codex SDK exists.
"""

import logging
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger(__name__)

_sessions: dict[str, dict] = {}


async def spawn(
    session_id: str,
    model: str,
    autopilot: bool = False,
    resume_sid: str | None = None,
    surface_prompt: str | None = None,
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "MIND_NAME",
    mind_name: str = "MIND_NAME",
    system_prompt_blocks: str = "",
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    is_group_session: bool = False,
) -> dict:
    if system_prompt_blocks and surface_prompt:
        full_prompt = f"{system_prompt_blocks}\n\n{surface_prompt}"
    elif surface_prompt:
        full_prompt = surface_prompt
    else:
        full_prompt = system_prompt_blocks
    state = {"system_prompt": full_prompt, "thread_id": resume_sid, "model": model, "mcp_config": mcp_config}
    _sessions[session_id] = state
    log.info("Session %s initialised (resume=%s)", session_id, resume_sid or "new")
    return state


async def send(session_id: str, content: str, images: list[dict] | None = None, db: Any = None) -> AsyncGenerator[dict, None]:
    raise NotImplementedError("Codex SDK does not exist yet. Use codex_cli_codex template instead.")


async def kill(session_id: str) -> None:
    _sessions.pop(session_id, None)
