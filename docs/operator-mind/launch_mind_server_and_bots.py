"""Systemd entry point — launches the mind server and its bots/surfaces
inside one asyncio event loop.

Reference template for the operator-mind install pattern. Canonical runtime copy is at
`<repo-root>/launch_mind_server_and_bots.py`; keep this in sync.

Today: mind_server.app + bots.telegram_bot.run_telegram_bot.
Future bots/surfaces (Discord, additional schedulers, etc.) are added
as further coroutines to the asyncio.gather() call below.

SIGTERM (or `systemctl stop <name>.service`) shuts everything down
gracefully via uvicorn's signal handling.

Gateway/sessions/broker live in hive-comms (NS-owned container) and
are reached over HTTP at COMMS_URL with COMMS_BEARER_TOKEN. Lucent
(vector store + KG) lives in the shared hive-lucent container
at LUCENT_URL_SELF.
"""
import asyncio
import os

import uvicorn

from mind_server import app as mind_app
from bots.telegram_bot import run_telegram_bot


async def main() -> None:
    mind_port = int(os.environ["MIND_SERVER_PORT"])

    mind_cfg = uvicorn.Config(
        mind_app, host="0.0.0.0", port=mind_port, log_level="info"
    )
    mind_server = uvicorn.Server(mind_cfg)

    await asyncio.gather(
        mind_server.serve(),
        run_telegram_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
