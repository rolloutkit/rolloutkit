"""What one experiment produced. Contracts read this and nothing else."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from preflightkit.config.models import Config
from preflightkit.engine.bus import EventBus
from preflightkit.runtime.base import (
    DaemonEvent,
    Pid1Facts,
    TeardownCalibration,
    daemon_interval_ms,
)
from preflightkit.traffic.baseline import Baseline, ReadinessBaseline
from preflightkit.traffic.client import RequestResult
from preflightkit.traffic.accept_probe import AcceptAttempt, AcceptOutcome
from preflightkit.probes.http import READINESS_POLL_INTERVAL_MS

#: Exit codes the kernel produces for a process killed by a signal (128 + signo).
SIGTERM_EXIT = 143
SIGKILL_EXIT = 137

SIGTERM_SIGNO = 15


@dataclass(slots=True)
class RunReport:
    config: Config
    bus: EventBus = field(default_factory=EventBus)

    # Startup
    container_start_overhead_ms: float | None = None
    container_started_ns: int | None = None
    tcp_open_ns: int | None = None
    readiness_ok_ns: int | None = None
    tcp_open_duration_ms: float | None = None
    startup_duration_observed_ms: float | None = None
    readiness_status: int | None = None
    startup_failure: str | None = None

    # Shutdown, all relative to sigterm_ns (T0)
    sigterm_ns: int | None = None
    readiness_drop_ns: int | None = None
    readiness_drop_status: int | None = None
    readiness_drop_mode: str | None = None  # "status_change" | "unreachable"
    exit_ns: int | None = None
    exit_code: int | None = None
    sigkill_sent: bool = False
    sigkill_ns: int | None = None

    #: Facts produced by contracts and consumed by engine-side precondition
    #: resolvers. Contracts declare names; they do not call each other.
    facts: dict[str, Any] = field(default_factory=dict)

    #: /proc/1/status from inside the container's PID namespace, read before the
    #: signal. None when no probe image was available or the daemon refused.
    pid1: Pid1Facts | None = None

    #: Frames from the daemon's own event stream. They carry dockerd's clock as
    #: well as ours, which is the only way to time an interval without our own
    #: round trip inside it.
    daemon_events: list[DaemonEvent] = field(default_factory=list)

    #: Kill-to-die for a container that could not have taken any time to die,
    #: measured in the same network shape as the target. The floor under every
    #: duration below. See DockerRuntime.measure_teardown_floor.
    teardown_floor_ms: float | None = None
    teardown_calibration: TeardownCalibration | None = None
    teardown_calibration_status: str = "unavailable"

    # Traffic
    baseline: Baseline | None = None
    readiness_baseline: ReadinessBaseline | None = None
    #: "configured", "readiness_fallback", or "disabled".
    inflight_target: str | None = None
    inflight_path: str | None = None
    inflight_fallback_p50_ms: float | None = None
    inflight_fallback_jitter_ms: float | None = None
    inflight_fallback_ratio: float | None = None
    inflight_measurement_enabled: bool = False
    sigterm_after_ms: int | None = None
    #: "config" when the user set sigterm_after, "baseline" when it was derived.
    sigterm_after_source: str | None = None
    requests: list[RequestResult] = field(default_factory=list)
    accept_attempts: list[AcceptAttempt] = field(default_factory=list)
    accept_probe_interval_ms: int = 0
    accept_refused_streak_target: int = 0
    accept_probe_stopped_ns: int | None = None

    # Environment / evidence
    image_config: dict[str, Any] = field(default_factory=dict)
    container_state: dict[str, Any] = field(default_factory=dict)
    logs_tail: str = ""
    ping_latencies_ns: list[int] = field(default_factory=list)
    docker_endpoint: str = ""
    docker_server: dict[str, Any] = field(default_factory=dict)
    host_os: str = ""
    load_average: tuple[float, float, float] | None = None
    cpu_count: int | None = None
    port_proxy_likely: bool = False
    network_name: str = ""
    traffic_endpoint: str = ""
    probe_location: str = "host_fallback"
    probe_fallback_reason: str | None = None
    probe_image: str | None = None
    probe_clock_alignment_ms: float | None = None
    phase_durations_ms: dict[str, float] = field(
        default_factory=lambda: {
            "probe_image_preparation": 0.0,
            "dependencies": 0.0,
            "target_start": 0.0,
            "baseline": 0.0,
            "calibration": 0.0,
            "experiment": 0.0,
            "teardown": 0.0,
        }
    )

    def offset_ms(self, timestamp_ns: int | None) -> float | None:
        """Milliseconds after T0. Negative values mean before the signal."""
        if timestamp_ns is None or self.sigterm_ns is None:
            return None
        return (timestamp_ns - self.sigterm_ns) / 1_000_000

    @property
    def measurement_jitter_ms(self) -> float | None:
        """Median timing floor measured at the active traffic location.

        The sidecar measures fresh TCP connection round trips to the target. The
        host fallback retains the Docker daemon round-trip calibration.
        """
        if not self.ping_latencies_ns:
            return None
        return statistics.median(self.ping_latencies_ns) / 1_000_000

    @property
    def sigkill_effective(self) -> bool:
        """Whether SIGKILL is what actually ended the process.

        `sigkill_sent` records what preflightkit did; this records what happened.
        The two come apart at short budgets: the enforcer fires, the process was
        already on its way out, and the daemon reports the process's own status.
        Judging on `sigkill_sent` would call a clean shutdown a kill — which is
        exactly the claim SP003 used to make.
        """
        return self.exit_code == SIGKILL_EXIT

    @property
    def accept_stopped_ns(self) -> int | None:
        """First refused connection after SIGTERM, before any forced kill."""
        if self.accept_probe_stopped_ns is not None:
            return self.accept_probe_stopped_ns
        if self.sigterm_ns is None:
            return None
        candidates = [
            request.finished_ns
            for request in self.requests
            if request.outcome.value == "refused"
            and request.finished_ns is not None
            and request.finished_ns >= self.sigterm_ns
            and (self.sigkill_ns is None or request.finished_ns < self.sigkill_ns)
        ]
        return min(candidates) if candidates else None

    @property
    def last_accepted_ns(self) -> int | None:
        accepted = [
            attempt.connected_ns
            for attempt in self.accept_attempts
            if attempt.connected_ns is not None
            and (self.sigkill_ns is None or attempt.connected_ns < self.sigkill_ns)
        ]
        return max(accepted) if accepted else None

    @property
    def accept_window_ms(self) -> float | None:
        if self.sigterm_ns is None or self.last_accepted_ns is None:
            return None
        return (self.last_accepted_ns - self.sigterm_ns) / 1_000_000

    @property
    def accept_then_reset(self) -> list[AcceptAttempt]:
        if self.sigterm_ns is None:
            return []
        return [
            attempt
            for attempt in self.accept_attempts
            # The request for a new connection must itself begin after T0.
            # A handshake started before T0 belongs to the already-routed
            # traffic population; destruction of those obligations is SP005's
            # concern, not SP004's.
            if attempt.started_ns >= self.sigterm_ns
            and attempt.connected_ns is not None
            and attempt.outcome is AcceptOutcome.RESET
        ]

    @property
    def startup_resolution_ms(self) -> float | None:
        """Resolution floor for startup budget comparisons.

        The container create/start round trip is outside the readiness interval,
        but its run-to-run spread bounds how precisely this harness can compare
        a startup measurement with a configured budget. Readiness is sampled,
        not observed continuously, so one polling interval belongs in the same
        resolution floor.
        """
        if self.container_start_overhead_ms is None:
            return None
        return self.container_start_overhead_ms + READINESS_POLL_INTERVAL_MS

    @property
    def readiness_drop_observation(self) -> str:
        return self.readiness_drop_mode or "never"

    @property
    def tcp_open_is_meaningful(self) -> bool:
        """Whether the TCP timestamp says anything about the application.

        The listen backlog completes handshakes before accept() is ever called.
        The fallback has a second possible limitation: Docker Desktop's
        published-port proxy opens before the application binds its listener.
        """
        return not self.port_proxy_likely

    @property
    def startup_duration_ms(self) -> float | None:
        if self.startup_duration_observed_ms is not None:
            return self.startup_duration_observed_ms
        if self.container_started_ns is None or self.readiness_ok_ns is None:
            return None
        return (self.readiness_ok_ns - self.container_started_ns) / 1_000_000

    # -- how long the shutdown took, from three different vantage points ---

    @property
    def observed_shutdown_duration_ms(self) -> float | None:
        """Our clock: the signal request going out, the exit coming back.

        Both ends are one round trip away from the event they describe, and on
        Docker Desktop the outbound one alone measured 6-13ms.
        """
        if self.sigterm_ns is None or self.exit_ns is None:
            return None
        return (self.exit_ns - self.sigterm_ns) / 1_000_000

    @property
    def daemon_shutdown_duration_ms(self) -> float | None:
        """The daemon's clock: signal delivered, container dead.

        Preferred, because both timestamps are stamped by dockerd itself. It
        removes our round trip from both ends and it is what a kubelet's own view
        would look like. What it does *not* remove is `teardown_floor_ms` — on
        Docker Desktop the daemon takes ~85ms to report the death of a container
        with a published port even when the death was instantaneous.
        """
        return daemon_interval_ms(self.daemon_events, "kill", "die")

    @property
    def shutdown_duration_source(self) -> str | None:
        if self.daemon_shutdown_duration_ms is not None:
            return "daemon_events"
        if self.observed_shutdown_duration_ms is not None:
            return "observed"
        return None

    @property
    def shutdown_duration_ms(self) -> float | None:
        """T4 - T0. The number every deadline verdict is built on."""
        daemon = self.daemon_shutdown_duration_ms
        return daemon if daemon is not None else self.observed_shutdown_duration_ms

    @property
    def observation_lag_ms(self) -> float | None:
        """What our vantage point added to the daemon's own figure."""
        daemon = self.daemon_shutdown_duration_ms
        observed = self.observed_shutdown_duration_ms
        if daemon is None or observed is None:
            return None
        return observed - daemon

    # -- what PID 1 had told the kernel it wanted, before the signal -------

    @property
    def pid1_comm(self) -> str | None:
        return self.pid1.comm if self.pid1 else None

    @property
    def runtime_handler_installed(self) -> bool | None:
        """Whether PID 1 had a SIGTERM handler installed. None if unmeasured.

        Measured before T0, and it predicts the outcome: the kernel discards a
        signal aimed at the init of a PID namespace when the disposition is still
        the default, so `False` here means the application will not see SIGTERM
        at all, however long the grace period is.
        """
        return self.pid1.catches(SIGTERM_SIGNO) if self.pid1 else None

    @property
    def sigterm_ignored(self) -> bool | None:
        """Whether PID 1 had explicitly set SIGTERM to SIG_IGN.

        A different defect from the one above with the same symptom. Here the
        application asked for the signal to be dropped; there it never asked for
        anything and the kernel dropped it on its behalf.
        """
        return self.pid1.ignores(SIGTERM_SIGNO) if self.pid1 else None

    @property
    def readiness_drop_delay_ms(self) -> float | None:
        return self.offset_ms(self.readiness_drop_ns)
