"""Runtime protocol, and the value objects a measurement is made of.

Contracts depend on this shape, never on Docker. A KubernetesRuntime or
ProcessRuntime implementing it needs no contract changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import statistics
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    image: str
    port: int | None
    env: dict[str, str] = field(default_factory=dict)
    command: list[str] | None = None
    name: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    memory_bytes: int = 1024 * 1024 * 1024
    nano_cpus: int = 2_000_000_000
    network_name: str | None = None
    network_aliases: tuple[str, ...] = ()
    publish_port: bool = False


@dataclass(frozen=True, slots=True)
class Container:
    id: str
    name: str
    host: str
    host_port: int
    container_ip: str = ""
    published_port: int | None = None


@dataclass(frozen=True, slots=True)
class Network:
    id: str
    name: str


class Runtime(Protocol):
    async def create_network(self, name: str) -> Network: ...

    async def remove_network(self, network: Network) -> None: ...

    async def start(self, spec: ContainerSpec) -> Container: ...

    async def signal(self, container: Container, sig: str) -> None: ...

    async def wait(self, container: Container, timeout_ms: int) -> int | None: ...

    async def logs(self, container: Container, tail: int = 50) -> str: ...

    async def inspect(self, container: Container) -> dict[str, Any]: ...

    async def remove(self, container: Container) -> None: ...


@dataclass(frozen=True, slots=True)
class DaemonEvent:
    """One frame from /events, carrying both clocks.

    `daemon_ns` is stamped by dockerd. `observed_ns` is when the frame reached
    us. The pair matters: subtracting two `daemon_ns` values measures an interval
    on a single clock with no round trip in it, which is the only way to say how
    long a container took to die without also measuring our own socket.
    """

    action: str
    daemon_ns: int
    observed_ns: int


@dataclass(frozen=True, slots=True)
class TeardownCalibration:
    """Distribution of daemon teardown overhead in the target's network shape."""

    samples_ms: tuple[float, ...]
    stddev_k: float = 3.0

    @property
    def floor_ms(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def stddev_ms(self) -> float:
        return statistics.stdev(self.samples_ms) if len(self.samples_ms) > 1 else 0.0

    @property
    def resolution_threshold_ms(self) -> float:
        return self.floor_ms + self.stddev_k * self.stddev_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples_ms": [round(value, 3) for value in self.samples_ms],
            "sample_count": len(self.samples_ms),
            "floor_statistic": "median",
            "floor_ms": round(self.floor_ms, 3),
            "stddev_statistic": "sample",
            "stddev_ms": round(self.stddev_ms, 3),
            "stddev_k": self.stddev_k,
            "resolution_threshold_ms": round(self.resolution_threshold_ms, 3),
        }


@dataclass(frozen=True, slots=True)
class Pid1Facts:
    """What /proc/1/status says about the process holding PID 1.

    Read from inside the target's PID namespace, before any signal is sent. The
    field that earns this its place is `sig_caught`: for the init of a PID
    namespace the kernel discards any signal whose disposition is still the
    default, so an application with no handler installed will not merely fail to
    clean up — it will not observe the signal at all. That is predictable from
    this bitmask *before* the experiment, and it is invisible in the exit code
    afterwards.
    """

    comm: str
    sig_caught: int
    sig_ignored: int
    sig_blocked: int

    def catches(self, signo: int) -> bool:
        return bool(self.sig_caught >> (signo - 1) & 1)

    def ignores(self, signo: int) -> bool:
        return bool(self.sig_ignored >> (signo - 1) & 1)

    def blocks(self, signo: int) -> bool:
        return bool(self.sig_blocked >> (signo - 1) & 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "comm": self.comm,
            "sig_caught": f"{self.sig_caught:016x}",
            "sig_ignored": f"{self.sig_ignored:016x}",
            "sig_blocked": f"{self.sig_blocked:016x}",
        }


def parse_proc_status(text: str) -> Pid1Facts | None:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if value:
            fields[key.strip()] = value.strip()
    if "SigCgt" not in fields:
        return None
    try:
        return Pid1Facts(
            comm=fields.get("Name", ""),
            sig_caught=int(fields["SigCgt"], 16),
            sig_ignored=int(fields.get("SigIgn", "0"), 16),
            sig_blocked=int(fields.get("SigBlk", "0"), 16),
        )
    except ValueError:
        return None


def daemon_interval_ms(
    frames: Sequence[DaemonEvent], start: str, end: str
) -> float | None:
    """Milliseconds between two daemon-stamped frames, on the daemon's clock.

    The first frame of each action wins. `kill` appears twice on a run where the
    budget ran out and SIGKILL followed, and the interval that means anything
    starts at the SIGTERM.
    """
    first: dict[str, DaemonEvent] = {}
    for frame in frames:
        first.setdefault(frame.action, frame)
    if start not in first or end not in first:
        return None
    return (first[end].daemon_ns - first[start].daemon_ns) / 1_000_000
