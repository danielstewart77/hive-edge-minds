"""What a mind can honestly say about the machine it is running on.

The console draws GPU, memory and disk per mind, and three things make that
harder than it looks. Two minds can share one machine (Skippy bare metal and
Mordecai in a container on this workstation), so the figures need a host
identity to collapse under. A container's root filesystem is its image
layer, not the disk anybody cares about. And "no GPU" has three causes that
a single answer would flatten: no device, no tool to ask with, and a tool
that failed — only the first means the machine has no GPU.
"""

from __future__ import annotations

import host_metrics


# Real output shape from
# `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total
#             --format=csv,noheader,nounits`
NVIDIA_CSV = "NVIDIA GeForce RTX 4090, 37, 3218, 24564\n"
NVIDIA_CSV_TWO = (
    "NVIDIA GeForce RTX 4090, 37, 3218, 24564\n"
    "NVIDIA GeForce RTX 3090, 0, 12, 24576\n"
)

MEMINFO = """MemTotal:       65792140 kB
MemFree:         2118344 kB
MemAvailable:   41003128 kB
Buffers:          912340 kB
"""


# --- GPU: three distinct absences ------------------------------------------


def test_a_reporting_gpu_gives_utilisation_and_both_memory_figures():
    state = host_metrics.parse_gpu(NVIDIA_CSV)

    assert state[0] == {
        "name": "NVIDIA GeForce RTX 4090",
        "utilization_percent": 37,
        "memory_used_mb": 3218,
        "memory_total_mb": 24564,
    }


def test_every_installed_card_is_reported_not_just_the_first():
    assert len(host_metrics.parse_gpu(NVIDIA_CSV_TWO)) == 2


def test_a_machine_with_no_card_is_absent_not_a_card_at_zero():
    """Requirement 17. A GPU sitting idle and a machine that has none are
    different facts, and only one of them means 'nothing to see here'."""
    state = host_metrics.gpu_state(run=lambda: ("", 0))

    assert state["status"] == "absent"
    assert state["devices"] == []


def test_a_missing_probe_tool_is_not_a_missing_gpu():
    """No `nvidia-smi` on a Windows box, or in a container without the
    toolkit, says nothing about whether a card is installed. Reporting
    'absent' there is a claim about hardware made from the absence of a
    binary."""

    def _missing():
        raise FileNotFoundError("nvidia-smi")

    assert host_metrics.gpu_state(run=_missing)["status"] == "tool_missing"


def test_a_probe_that_fails_is_distinct_from_both():
    """A driver that fell over mid-day must not read as a machine that never
    had a card — that is the one state somebody needs to act on."""

    def _broken():
        return ("Failed to initialize NVML: Driver/library version mismatch", 9)

    state = host_metrics.gpu_state(run=_broken)

    assert state["status"] == "query_failed"
    assert "NVML" in state["error"]


def test_a_reporting_probe_carries_its_devices_through():
    state = host_metrics.gpu_state(run=lambda: (NVIDIA_CSV, 0))

    assert state["status"] == "reporting"
    assert state["devices"][0]["utilization_percent"] == 37


def test_a_garbled_gpu_row_is_dropped_rather_than_reported_as_zero():
    """A row that does not parse is not a card at zero use."""
    devices = host_metrics.parse_gpu("NVIDIA Something, n/a, n/a, n/a\n" + NVIDIA_CSV)

    assert len(devices) == 1
    assert devices[0]["utilization_percent"] == 37


# --- Memory -----------------------------------------------------------------


def test_memory_reports_used_and_total_separately():
    """Used is total minus *available*, not minus free: free excludes the page
    cache, so a healthy Linux box would report 97% used forever."""
    state = host_metrics.memory_from_meminfo(MEMINFO)

    assert state["total_bytes"] == 65792140 * 1024
    assert state["used_bytes"] == (65792140 - 41003128) * 1024


def test_memory_that_cannot_be_read_reports_unavailable_rather_than_zero():
    state = host_metrics.memory_from_meminfo("")

    assert state["status"] == "unavailable"
    assert state.get("used_bytes") is None


# --- Disk -------------------------------------------------------------------


def test_disk_reports_used_and_total_for_the_mount_it_names():
    state = host_metrics.disk_state(
        usage=lambda path: (500, 300, 200), mount="/", host_root_mounted=False
    )

    assert state["total_bytes"] == 500
    assert state["used_bytes"] == 300
    assert state["mount"] == "/"


def test_a_container_says_its_disk_figure_is_not_the_hosts():
    """Requirement 25. Inside a container `/` is the image layer. Reporting
    it unqualified puts a 4 GB disk on the dashboard for a machine with 4 TB."""
    state = host_metrics.disk_state(
        usage=lambda path: (500, 300, 200), mount="/", host_root_mounted=False
    )

    assert state["reflects_host"] is False


def test_a_container_with_the_host_root_mounted_reports_the_host_disk():
    state = host_metrics.disk_state(
        usage=lambda path: (500, 300, 200), mount="/host", host_root_mounted=True
    )

    assert state["reflects_host"] is True
    assert state["mount"] == "/host"


def test_a_disk_that_cannot_be_read_reports_unavailable_rather_than_zero():
    def _boom(path):
        raise OSError("gone")

    state = host_metrics.disk_state(usage=_boom, mount="/", host_root_mounted=True)

    assert state["status"] == "unavailable"
    assert state.get("total_bytes") is None


# --- Host identity ----------------------------------------------------------


def test_two_minds_on_one_machine_report_the_same_host_id():
    """The dedup key. A container sees the host's boot id verbatim, which is
    what makes a bare-metal mind and a containerised one on the same
    workstation collapse to a single row instead of listing that machine
    twice with the container's overlay as its disk."""
    bare = host_metrics.host_identity(boot_id="913a9fdb", hostname="workstation")
    contained = host_metrics.host_identity(boot_id="913a9fdb", hostname="a3f91c2b4e10")

    assert bare["host_id"] == contained["host_id"]


def test_two_machines_report_different_host_ids():
    a = host_metrics.host_identity(boot_id="913a9fdb", hostname="workstation")
    b = host_metrics.host_identity(boot_id="00000000", hostname="workstation")

    assert a["host_id"] != b["host_id"]


def test_a_host_with_no_boot_id_falls_back_to_its_hostname():
    """Windows has no boot id file. A mind there still needs to be keyed to
    something, and its hostname is the honest second choice."""
    identity = host_metrics.host_identity(boot_id=None, hostname="ARNOLD-PC")

    assert identity["host_id"] == "ARNOLD-PC"


# --- The whole reading ------------------------------------------------------


def test_the_reading_says_when_it_was_taken():
    """Requirement 27: a page whose refresh silently failed must age rather
    than keep presenting the last numbers as current."""
    reading = host_metrics.collect(now=1_700_000_000.0)

    assert reading["observed_at"] == 1_700_000_000.0


def test_the_reading_always_carries_every_section():
    """A section dropped on failure is one the console cannot render a state
    for — it would have to guess between 'not supported' and 'broken'."""
    reading = host_metrics.collect(now=1.0)

    assert {"host", "gpu", "memory", "disk", "observed_at"} <= set(reading)


class TestTheAbsentCaseAsTheDriverActuallyReportsIt:
    def test_a_machine_with_no_card_says_absent_despite_a_non_zero_exit(self):
        """Real `nvidia-smi` on a GPU-less box with drivers installed exits
        non-zero and says so in words. Taking the exit code at face value
        puts "GPU probe failed" on a machine that is working correctly, and
        buries the real failures in the same bucket."""
        state = host_metrics.gpu_state(run=lambda: ("No devices were found", 6))

        assert state["status"] == "absent"

    def test_output_that_does_not_parse_is_a_failed_query_not_an_absent_gpu(self):
        """Saying "no GPU in this machine" about a box full of them, because
        one column arrived in a format this code has not seen, is the
        confident kind of wrong."""
        state = host_metrics.gpu_state(run=lambda: ("NVIDIA X, [N/A], [N/A], [N/A]", 0))

        assert state["status"] == "query_failed"

    def test_a_genuinely_empty_answer_is_absent(self):
        assert host_metrics.gpu_state(run=lambda: ("   \n", 0))["status"] == "absent"


class TestTheProbeCannotHangTheMind:
    def test_the_probe_is_given_a_deadline(self):
        """`GET /host` is served by the same process serving this mind's
        conversations. A wedged driver with no ceiling holds them all."""
        seen = {}

        class _Done:
            returncode = 0
            stdout = NVIDIA_CSV
            stderr = ""

        def _run(args, **kwargs):
            seen.update(kwargs)
            return _Done()

        import subprocess

        original = subprocess.run
        subprocess.run = _run
        try:
            host_metrics._run_nvidia_smi()
        finally:
            subprocess.run = original

        assert seen["timeout"] == host_metrics.GPU_TIMEOUT_SECONDS

    def test_a_probe_that_times_out_reports_rather_than_raising(self):
        import subprocess

        def _run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        original = subprocess.run
        subprocess.run = _run
        try:
            output, code = host_metrics._run_nvidia_smi()
        finally:
            subprocess.run = original

        assert code != 0
        assert "timed out" in output


class TestTheDiskDefaultsToTheHostWhenItCanSeeIt:
    def test_a_container_with_the_host_root_mounted_measures_that_mount(self):
        """Defaulting to `/` inside a container measures the image layer
        while still claiming to reflect the host — a 4 GB disk drawn for a
        machine holding 4 TB, which is the failure this module exists to
        prevent."""
        asked = []

        def _usage(path):
            asked.append(path)
            return (1, 1, 0)

        host_metrics.disk_state(usage=_usage, host_root_mounted=True)

        assert asked == ["/host"]

    def test_a_host_without_that_mount_measures_its_own_root(self):
        asked = []

        def _usage(path):
            asked.append(path)
            return (1, 1, 0)

        host_metrics.disk_state(usage=_usage, host_root_mounted=False)

        assert asked and asked[0] not in ("/host",)
