#!/usr/bin/env python3
"""Update the shared codex CLI install and report the version change.

Standalone stateless tool. No external dependencies (stdlib only).

Codex installs to a hivemind-owned NPM_CONFIG_PREFIX (see Dockerfile) instead
of npm's default root-owned global dir, so a plain `npm install -g
@openai/codex@latest` works at runtime as the non-root hivemind user. That
prefix is also where every codex-harness container on a host mounts the
shared `codex-global` volume, so running this tool once — in-process, or via
`--container` against any sibling container on the same host — updates codex
everywhere that volume is mounted, without a rebuild or a per-container repeat.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys


def _docker_bin() -> str:
    """Prefer the operator's borrowed host binary (see docker-compose.yml's
    /host mount) over any docker CLI baked into this image, since only the
    host binary is guaranteed to exist here."""
    for candidate in ("/host/usr/bin/docker", "/usr/bin/docker"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("docker")
    if found:
        return found
    raise FileNotFoundError("no docker binary found (checked /host/usr/bin/docker, PATH)")


def _exec_prefix(container: str | None) -> list[str]:
    if not container:
        return []
    return [_docker_bin(), "exec", container]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _installed_version(exec_prefix: list[str]) -> str | None:
    result = _run([*exec_prefix, "npm", "list", "-g", "@openai/codex", "--depth=0", "--json"])
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return data.get("dependencies", {}).get("@openai/codex", {}).get("version")


def update_codex(container: str | None = None) -> dict:
    exec_prefix = _exec_prefix(container)
    before = _installed_version(exec_prefix)
    install = _run([*exec_prefix, "npm", "install", "-g", "@openai/codex@latest"])
    after = _installed_version(exec_prefix)
    return {
        "container": container,
        "before_version": before,
        "after_version": after,
        "updated": bool(after) and after != before,
        "install_ok": install.returncode == 0,
        "install_stderr": install.stderr.strip() if install.returncode != 0 else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the shared codex CLI install")
    parser.add_argument(
        "--container",
        default=None,
        help="docker container to run the update in (omit to update in-process)",
    )
    parser.add_argument("--test-mode", action="store_true", help="skip the real npm calls")
    args = parser.parse_args()

    if args.test_mode:
        result = {
            "container": args.container,
            "before_version": "0.0.0",
            "after_version": "0.0.1",
            "updated": True,
            "install_ok": True,
            "install_stderr": "",
        }
        print(json.dumps(result))
        return 0

    try:
        result = update_codex(container=args.container)
    except FileNotFoundError as e:
        print(json.dumps({"container": args.container, "install_ok": False, "install_stderr": str(e)}))
        return 1

    print(json.dumps(result))
    return 0 if result["install_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
