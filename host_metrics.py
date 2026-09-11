"""What this mind can honestly say about the machine it runs on.

The console draws GPU, memory and disk per mind, and the only party that can
see any of it is the mind itself — a container in the stack, a bare-metal
mind here, and a mind on a Windows box across the LAN are one code path
because each reports its own host rather than a bind mount reaching in.

Three things make that harder than it looks, and each is a decision below:

*Two minds can share one machine.* Skippy runs bare metal on this
workstation and Mordecai runs in a container on the same one. Listing that
machine twice is wrong twice over — once for the duplicate, once because the
container's root filesystem is its image layer and not the disk anybody
means. The readings are therefore keyed to a **host identity** the console
collapses on, and the container's boot id is the host's boot id verbatim,
which is what makes the collapse correct rather than a name-matching guess.

*A disk figure from inside a container is about the image.* It is still
reported — an unqualified number would be a lie, an absent one would be a
hole — but it says whether it reflects the host.

*"No GPU" has three causes.* No device, no tool to ask with, and a tool that
failed. Flattening them means a driver that fell over at lunchtime reads as
a machine that never had a card, and the one state somebody needs to act on
is the one that disappears.

Nothing here raises. A reading is a set of states, and "I could not tell"
is one of them; a collector that threw would take the whole page down over
a machine that merely lacked a file.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from typing import Callable, Optional

#: Hard ceiling on the GPU probe. It runs on every snapshot, and a wedged
#: `nvidia-smi` must not hold the response open past the console's own
#: per-host deadline — a slow host and an unreachable one look identical to
#: a reader, which is the confusion requirement 16 exists to prevent.
GPU_TIMEOUT_SECONDS = 2.0

_NVIDIA_QUERY = (
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
)

#: Where a containerised mind is given the host's root, when it is given it
#: at all. The operator-mind pattern mounts the host at `/host`; a mind
#: without it reports its own overlay and says so.
_HOST_ROOT = "/host"


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------


def parse_gpu(csv_text: str) -> list[dict]:
    """One entry per card `nvidia-smi` listed.

    A row that does not parse is dropped rather than reported with zeroes:
    a card at 0% and a card whose numbers were unreadable are different
    things, and the second must not be drawn as an idle GPU.
    """
    devices = []
    for line in csv_text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        name, utilization, used, total = parts
        try:
            devices.append(
                {
                    "name": name,
                    "utilization_percent": int(utilization),
                    "memory_used_mb": int(used),
                    "memory_total_mb": int(total),
                }
            )
        except ValueError:
            continue
    return devices


def _run_nvidia_smi() -> tuple[str, int]:
    """Ask the driver. Raises `FileNotFoundError` when the tool is absent."""
    try:
        completed = subprocess.run(
            _NVIDIA_QUERY,
            capture_output=True,
            text=True,
            timeout=GPU_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ("nvidia-smi timed out", 124)
    output = completed.stdout if completed.returncode == 0 else completed.stderr
    return (output, completed.returncode)


def gpu_state(run: Optional[Callable[[], tuple[str, int]]] = None) -> dict:
    """The GPU, in one of four states a reader can act on differently.

    - `reporting` — cards, with their numbers.
    - `absent` — the tool ran and found nothing. The machine has no card.
    - `tool_missing` — no `nvidia-smi`. Says nothing about the hardware, and
      must not be rendered as "no GPU": a Windows box, an AMD card, or a
      container without device passthrough all land here on machines that
      may be full of GPUs.
    - `query_failed` — the tool ran and broke. A driver mismatch belongs in
      front of somebody, not folded into a shrug.
    """
    # Resolved at call time, not bound as a default: a default argument
    # freezes the probe at import, so patching the module attribute — which
    # is how the route's own tests simulate a wedged driver — would silently
    # keep calling the real one.
    run = run or _run_nvidia_smi
    try:
        output, code = run()
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        return {"status": "tool_missing", "devices": [], "error": str(exc)}
    except OSError as exc:
        return {"status": "query_failed", "devices": [], "error": str(exc)}

    if code != 0:
        return {"status": "query_failed", "devices": [], "error": output.strip()}
    devices = parse_gpu(output)
    if not devices:
        return {"status": "absent", "devices": []}
    return {"status": "reporting", "devices": devices}


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


def memory_from_meminfo(text: str) -> dict:
    """Used and total, from `/proc/meminfo`.

    Used is total minus **MemAvailable**, not minus MemFree. Free excludes
    the page cache, which Linux fills with reclaimable data by design — a
    box with 40 GB available would report itself at 97% used forever, and
    the one number on the panel that means "act now" would mean nothing.
    """
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                values[key.strip()] = int(parts[0]) * 1024
            except ValueError:
                continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return {"status": "unavailable", "used_bytes": None, "total_bytes": None}
    return {
        "status": "reporting",
        "used_bytes": total - available,
        "total_bytes": total,
    }


def _windows_memory() -> dict:
    """Windows has no `/proc`. Ask the kernel directly.

    The kid boxes run as `windows-task` installs, and a memory panel that
    only ever works on Linux would quietly report two of the family's
    machines as unreadable forever.
    """
    try:
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"status": "unavailable", "used_bytes": None, "total_bytes": None}
        return {
            "status": "reporting",
            "used_bytes": status.ullTotalPhys - status.ullAvailPhys,
            "total_bytes": status.ullTotalPhys,
        }
    except Exception as exc:  # noqa: BLE001 — a reading, never a fault
        return {
            "status": "unavailable",
            "used_bytes": None,
            "total_bytes": None,
            "error": str(exc),
        }


def memory_state() -> dict:
    """This machine's physical memory, however this platform reports it."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            return memory_from_meminfo(handle.read())
    except OSError:
        pass
    if platform.system() == "Windows":
        return _windows_memory()
    return {"status": "unavailable", "used_bytes": None, "total_bytes": None}


# --------------------------------------------------------------------------
# Disk
# --------------------------------------------------------------------------


def disk_state(
    usage: Callable[[str], tuple[int, int, int]] = shutil.disk_usage,
    mount: Optional[str] = None,
    host_root_mounted: Optional[bool] = None,
) -> dict:
    """Used and total for the filesystem this mind can actually see.

    `reflects_host` is the whole point of the extra field. A containerised
    mind reading `/` reads its image layer, and a four-gigabyte disk drawn
    for a machine holding four terabytes is worse than no disk panel at all —
    it is a number somebody would act on.
    """
    if host_root_mounted is None:
        host_root_mounted = os.path.isdir(_HOST_ROOT)
    if mount is None:
        mount = _HOST_ROOT if host_root_mounted else os.path.abspath(os.sep)
    try:
        total, used, _free = usage(mount)
    except OSError as exc:
        return {
            "status": "unavailable",
            "mount": mount,
            "used_bytes": None,
            "total_bytes": None,
            "reflects_host": bool(host_root_mounted),
            "error": str(exc),
        }
    return {
        "status": "reporting",
        "mount": mount,
        "used_bytes": used,
        "total_bytes": total,
        "reflects_host": bool(host_root_mounted),
    }


# --------------------------------------------------------------------------
# Host identity
# --------------------------------------------------------------------------


def _boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            return "docker" in handle.read() or "containerd" in handle.read()
    except OSError:
        return False


#: "Caller said nothing", which is not the same as "this host has none".
#: Windows genuinely has no boot id, and a `None` that fell through to
#: reading the real file would make that case untestable and would key a
#: Windows mind to whatever the *collector's* machine happened to report.
_UNSET = object()


def host_identity(boot_id=_UNSET, hostname=_UNSET) -> dict:
    """Who this machine is, in a form two minds on it will agree on.

    The boot id is the key because a container sees the *host's* boot id
    verbatim — verified on this workstation, where the value inside
    `hive-comms` matches the one on the host byte for byte. Hostname cannot
    do this job: a container's hostname is its own short container id, so
    keying on it would list one physical machine twice and draw the
    container's overlay as its disk.

    Windows has no such file, so the hostname is the honest fallback there:
    one mind per kid's box, and nothing to collapse.
    """
    boot_id = _boot_id() if boot_id is _UNSET else boot_id
    hostname = socket.gethostname() if hostname is _UNSET else hostname
    return {
        "host_id": boot_id or hostname,
        "hostname": hostname,
        "containerized": in_container(),
        "platform": platform.system(),
    }


def collect(now: Optional[float] = None) -> dict:
    """One reading of this host, with the time it was taken.

    Every section is always present. A section dropped on failure is one the
    console cannot render a state for — it would be left guessing between
    "this platform does not support it" and "the probe broke", which is the
    distinction the states inside each section exist to make.
    """
    return {
        "host": host_identity(),
        "gpu": gpu_state(),
        "memory": memory_state(),
        "disk": disk_state(),
        "observed_at": time.time() if now is None else now,
    }
