"""What /proc/1/status says, and what happens when nothing says it.

The bitmasks below were copied out of real containers rather than constructed.
They are the reason SP003 can tell "the application ignored the signal" apart
from "the application never got the signal", and the reason it can say which
before the signal is sent.
"""

from __future__ import annotations

import anyio
import pytest

from rolloutkit.runtime.base import (
    Container,
    DaemonEvent,
    daemon_interval_ms,
    parse_proc_status,
)
from rolloutkit.runtime.docker import DockerRuntime

SIGTERM = 15
SIGINT = 2

#: CPython, `python3 -u server.py` on scratch-adjacent images. Catches SIGINT and
#: nothing else: the interpreter installs a handler for KeyboardInterrupt and
#: leaves every other disposition at the default. As PID 1 that means SIGTERM is
#: discarded by the kernel before the process is ever scheduled.
CPYTHON = """Name:\tpython
State:\tS (sleeping)
Pid:\t1
SigBlk:\t0000000000000000
SigIgn:\t0000000000000000
SigCgt:\t0000000000000002
"""

#: A Go binary built from a program with no signal.Notify at all. The runtime
#: installs handlers for essentially every signal whether the program asked or
#: not, so the mask says "caught" while the application has no shutdown path.
GO_RUNTIME = """Name:\tserver
State:\tS (sleeping)
Pid:\t1
SigBlk:\t0000000000000000
SigIgn:\t0000000000000000
SigCgt:\tfffffffd7fc1feff
"""

#: uvicorn. A real handler, installed by the application.
UVICORN = """Name:\tuvicorn
State:\tS (sleeping)
Pid:\t1
SigBlk:\t0000000000000000
SigIgn:\t0000000000000000
SigCgt:\t0000000100004002
"""


def test_a_default_disposition_is_visible_before_the_signal() -> None:
    facts = parse_proc_status(CPYTHON)
    assert facts is not None
    assert facts.comm == "python"
    assert facts.catches(SIGINT)
    assert not facts.catches(SIGTERM)


def test_a_language_runtime_counts_as_a_handler() -> None:
    """It is a handler. Whether it shuts anything down is SP005's question.

    Worth stating because it is the difference between the two Go fixtures and
    the Python one: the Go binary with no signal.Notify still receives SIGTERM,
    and then dies of it with exit 2 instead of being discarded into a full grace
    period.
    """
    facts = parse_proc_status(GO_RUNTIME)
    assert facts is not None
    assert facts.catches(SIGTERM)


def test_an_application_handler_reads_the_same_way() -> None:
    facts = parse_proc_status(UVICORN)
    assert facts is not None
    assert facts.catches(SIGTERM) and not facts.ignores(SIGTERM)


def test_an_explicit_sig_ign_is_a_different_fact() -> None:
    facts = parse_proc_status(CPYTHON.replace("SigIgn:\t0000000000000000", "SigIgn:\t0000000000004000"))
    assert facts is not None
    assert facts.ignores(SIGTERM) and not facts.catches(SIGTERM)


@pytest.mark.parametrize(
    "text",
    ["", "not a proc file", "Name:\tx\nSigCgt:\tnot-hex\n", "Name:\tx\n"],
)
def test_unreadable_input_is_no_measurement_rather_than_a_wrong_one(text: str) -> None:
    assert parse_proc_status(text) is None


def test_the_probe_degrades_when_no_probe_image_is_present() -> None:
    """rolloutkit does not pull. A diagnosis that quietly reached the network
    would make an offline run behave differently from an online one."""

    async def scenario() -> None:
        runtime = DockerRuntime.__new__(DockerRuntime)

        async def absent(_image: str) -> bool:
            return False

        runtime.image_exists = absent  # type: ignore[method-assign]
        container = Container(id="abc", name="c", host="127.0.0.1", host_port=1)
        assert await runtime.probe_pid1(container) is None
        assert await runtime.measure_teardown_floor(
            port=8000, network_name="rk-test", publish_port=False
        ) is None

    anyio.run(scenario)


def test_teardown_calibration_uses_five_samples_and_measured_spread() -> None:
    async def scenario() -> None:
        runtime = DockerRuntime.__new__(DockerRuntime)
        samples = iter([79.0, 82.0, 85.0, 88.0, 92.0])
        calls = 0

        async def measure_once(
            *, port: int, network_name: str, publish_port: bool, timeout_ms: int
        ) -> float:
            nonlocal calls
            calls += 1
            assert port == 8000
            assert network_name == "rk-test"
            assert publish_port is False
            assert timeout_ms == 15_000
            return next(samples)

        runtime._measure_teardown_once = measure_once  # type: ignore[method-assign]
        calibration = await runtime.measure_teardown_floor(
            port=8000, network_name="rk-test", publish_port=False
        )

        assert calibration is not None
        assert calls == 5
        assert calibration.floor_ms == 85.0
        assert calibration.stddev_ms > 0
        assert calibration.resolution_threshold_ms == pytest.approx(
            calibration.floor_ms + 3 * calibration.stddev_ms
        )

    anyio.run(scenario)


def test_an_interval_needs_both_of_its_frames() -> None:
    frames = [DaemonEvent("kill", daemon_ns=1_000_000_000, observed_ns=0)]
    assert daemon_interval_ms(frames, "kill", "die") is None
    assert daemon_interval_ms([], "kill", "die") is None
