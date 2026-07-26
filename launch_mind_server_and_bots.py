"""Entry point — launches the mind server and its surface bots inside one
asyncio event loop.

Which surfaces run comes from `minds/$MIND_NAME/runtime.yaml` (`surfaces:`).
An empty list means the mind is reachable only through hive-comms — its
bot runs elsewhere (e.g. in the hive's own stack).

SIGTERM (systemd stop, docker stop, schtasks /end) shuts everything down
gracefully via uvicorn's signal handling.

Gateway/sessions/broker live in the hive-comms container and are reached
over HTTP at COMMS_URL with COMMS_BEARER_TOKEN. Lucent (vector store + KG)
lives in the hive-lucent container at LUCENT_URL_SELF.
"""
import asyncio
import os
from pathlib import Path

import uvicorn
import yaml

from mind_server import app as mind_app

PROJECT_DIR = Path(__file__).resolve().parent

_SURFACE_RUNNERS = {
    "telegram": lambda: __import__("bots.telegram_bot", fromlist=["run_telegram_bot"]).run_telegram_bot(),
    "discord": lambda: __import__("bots.discord_bot", fromlist=["run_discord_bot"]).run_discord_bot(),
}


def configured_surfaces() -> list[str]:
    """The runtime.yaml surfaces list for this mind; telegram if unreadable.

    The fallback keeps a pre-runtime.yaml install working unchanged — the
    telegram-only single mind is what every edge mind was before the contract
    carried a surfaces list.
    """
    mind_name = os.environ.get("MIND_NAME", "")
    runtime = PROJECT_DIR / "minds" / mind_name / "runtime.yaml"
    try:
        loaded = yaml.safe_load(runtime.read_text()) or {}
    except OSError:
        return ["telegram"]
    surfaces = loaded.get("surfaces")
    if surfaces is None:
        return ["telegram"]
    return [s for s in surfaces if s in _SURFACE_RUNNERS]


async def main() -> None:
    mind_port = int(os.environ["MIND_SERVER_PORT"])

    mind_cfg = uvicorn.Config(
        mind_app, host="0.0.0.0", port=mind_port, log_level="info"
    )
    mind_server = uvicorn.Server(mind_cfg)

    coros = [mind_server.serve()]
    coros.extend(_SURFACE_RUNNERS[s]() for s in configured_surfaces())
    await asyncio.gather(*coros)


if __name__ == "__main__":
    asyncio.run(main())
