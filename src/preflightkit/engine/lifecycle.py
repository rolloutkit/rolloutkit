"""The experiment itself: start, reach ready, load, SIGTERM, observe the exit.

Ordering matters more than it looks. The exit waiter is armed *before* SIGTERM is
sent, so T4 is the moment the daemon reports the exit rather than the moment we
got around to asking about it.
"""

from __future__ import annotations

import os
import platform
import uuid

import anyio
import httpx

from preflightkit.config.models import Config, DrainStrategy
from preflightkit.engine.context import RunReport
from preflightkit.engine.events import Kind, event, now_ns
from preflightkit.probes.http import probe_http, wait_for_readiness, wait_for_tcp
from preflightkit.runtime.base import Container, ContainerSpec, DaemonEvent
from preflightkit.runtime.docker import DockerError, DockerRuntime
from preflightkit.traffic.baseline import measure_baseline, measure_readiness_baseline
from preflightkit.traffic.accept_probe import (
    ACCEPT_PROBE_INTERVAL_MS,
    TERMINAL_REFUSED_STREAK,
    AcceptOutcome,
    probe_new_connection,
)
from preflightkit.traffic.generator import run_long_requests
from preflightkit.contracts.inflight import MIN_JITTER_RATIO

READINESS_WATCH_INTERVAL_MS = 20

#: Daemon events subscribed to for the shutdown window. `kill` dates the signal
#: on the daemon's own clock, `die` dates the exit on the same one.
_WATCHED_ACTIONS = ("kill", "die")

#: How long to keep the stream open after the exit is already known, waiting for
#: the frame that dates it. Measured at 1-2ms; ten times that is patience enough.
_DIE_FRAME_GRACE_MS = 500


#: Log lines kept when startup fails. Long enough to survive a
#: multi-worker crash, bounded because the image is untrusted.
_LOG_TAIL = 120

# A long shutdown budget is safely above the host-local teardown envelope seen
# in practice. Avoid five auxiliary container cycles when calibration cannot
# affect whether the deadline is resolvable.
TEARDOWN_CALIBRATION_MAX_BUDGET_MS = 2_000


class StartupFailure(Exception):
    """The experiment never got off the ground. Exit code 3, never a verdict.

    Carries the container's log tail. When a container dies during startup the
    reason is in its own output, and the container is removed right afterwards —
    a tool that swallows those lines forces the user to reproduce the whole run
    by hand with `docker run` just to read them.
    """

    def __init__(self, message: str, logs: str = "") -> None:
        super().__init__(message)
        self.logs = logs


async def run_experiment(config: Config, runtime: DockerRuntime) -> RunReport:
    report = RunReport(config=config)
    _record_host(report)
    report.docker_endpoint = f"{runtime.endpoint.socket_path} ({runtime.endpoint.source})"
    report.docker_server = {
        "version": runtime.server_info.get("Version"),
        "os": runtime.server_info.get("Os"),
        "arch": runtime.server_info.get("Arch"),
    }
    report.ping_latencies_ns = await runtime.ping_latency_ns()
    report.port_proxy_likely = _port_proxy_likely(report)

    try:
        report.image_config = (await runtime.inspect_image(config.target.image)).get("Config", {})
    except DockerError:
        report.image_config = {}

    run_token = uuid.uuid4().hex[:12]
    network = await runtime.create_network(f"pfk-{run_token}")
    report.network_name = network.name
    started: list[Container] = []
    container: Container | None = None
    try:
        for service_name, service in config.services.items():
            dependency = await runtime.start(
                ContainerSpec(
                    image=service.image,
                    port=None,
                    env=dict(service.env),
                    command=service.command,
                    name=f"pfk-{run_token}-{service_name}",
                    network_name=network.name,
                    network_aliases=(service_name,),
                )
            )
            started.append(dependency)

        container_start_requested_ns = now_ns()
        container = await runtime.start(
            ContainerSpec(
                image=config.target.image,
                port=config.target.port,
                env=dict(config.target.env),
                command=config.target.command,
                name=f"pfk-{run_token}-target",
                network_name=network.name,
                network_aliases=("target",),
                publish_port=report.port_proxy_likely,
            )
        )
        started.append(container)
        container_started_observed_ns = now_ns()
        report.container_start_overhead_ms = (
            container_started_observed_ns - container_start_requested_ns
        ) / 1_000_000
        report.traffic_endpoint = f"{container.host}:{container.host_port}"
        report.container_started_ns = container_started_observed_ns
        report.bus.record(
            event(
                Kind.CONTAINER_STARTED,
                timestamp_ns=report.container_started_ns,
                container=container.name,
                endpoint=report.traffic_endpoint,
                container_ip=container.container_ip,
                published_port=container.published_port,
            )
        )

        await _startup(config, runtime, container, report)
        await _calibrate(
            runtime,
            container,
            report,
            config.target.port,
            network.name,
            report.port_proxy_likely,
        )
        await _baseline(config, runtime, container, report)
        await _shutdown(config, runtime, container, report)
    finally:
        if container is not None:
            try:
                report.logs_tail = await runtime.logs(container, tail=_LOG_TAIL)
                report.container_state = (await runtime.inspect(container)).get("State", {})
            except Exception:  # noqa: BLE001 - cleanup must not mask the real error
                pass
        for running in reversed(started):
            try:
                await runtime.remove(running)
            except Exception:  # noqa: BLE001
                pass
        try:
            await runtime.remove_network(network)
        except Exception:  # noqa: BLE001
            pass

    return report


def _port_proxy_likely(report: RunReport) -> bool:
    """Is a userspace proxy sitting in front of the published port?

    Docker Desktop runs the daemon inside a Linux VM. Published ports reach it
    through a forwarder that accepts connections on the host before — and after —
    the container itself does. Timing taken through it describes the proxy.
    """
    daemon_os = (report.docker_server.get("os") or "").lower()
    host = report.host_os.split()[0].lower() if report.host_os else ""
    return host not in ("", "linux") or (daemon_os and daemon_os != host)


def _record_host(report: RunReport) -> None:
    report.host_os = f"{platform.system()} {platform.release()}"
    report.cpu_count = os.cpu_count()
    try:
        report.load_average = os.getloadavg()
    except OSError:
        report.load_average = None


async def _startup(
    config: Config, runtime: DockerRuntime, container: Container, report: RunReport
) -> None:
    base = f"http://{container.host}:{container.host_port}"

    tcp_ns = await wait_for_tcp(
        container.host, container.host_port, budget_ms=config.timeouts.startup
    )
    if tcp_ns is None:
        state = (await runtime.inspect(container)).get("State", {})
        report.container_state = state
        report.logs_tail = await runtime.logs(container, tail=_LOG_TAIL)
        if not state.get("Running", False):
            raise StartupFailure(
                f"container exited during startup with code {state.get('ExitCode')}. "
                "This is an environment problem (missing dependency, bad config), "
                "not a contract failure.",
                logs=report.logs_tail,
            )
        raise StartupFailure(
            f"port {config.target.port} never opened within "
            f"{config.timeouts.startup}ms",
            logs=report.logs_tail,
        )
    report.tcp_open_ns = tcp_ns
    report.bus.record(event(Kind.PORT_OPENED, timestamp_ns=tcp_ns, port=config.target.port))

    async with httpx.AsyncClient(base_url=base) as client:
        remaining = config.timeouts.startup - int(
            (tcp_ns - (report.container_started_ns or tcp_ns)) / 1_000_000
        )
        ready_ns, probe = await wait_for_readiness(
            client,
            config.probes.readiness.path,
            expected_status=config.probes.readiness.expected_status,
            budget_ms=max(remaining, 1000),
        )
        if ready_ns is None:
            detail = (
                f"status {probe.status}" if probe and probe.status else (probe.error if probe else "no response")
            )
            report.bus.record(event(Kind.READINESS_FAILED, detail=detail))
            state = (await runtime.inspect(container)).get("State", {})
            report.container_state = state
            report.logs_tail = await runtime.logs(container, tail=_LOG_TAIL)
            # On Docker Desktop the port proxy accepts connections even when
            # nothing is behind it, so wait_for_tcp can succeed against a
            # container that already died. Report the death, not the symptom.
            if not state.get("Running", False):
                raise StartupFailure(
                    f"container exited during startup with code "
                    f"{state.get('ExitCode')} — readiness "
                    f"{config.probes.readiness.path} never passed",
                    logs=report.logs_tail,
                )
            raise StartupFailure(
                f"readiness {config.probes.readiness.path} never returned "
                f"{config.probes.readiness.expected_status} (last: {detail})",
                logs=report.logs_tail,
            )
        report.readiness_ok_ns = ready_ns
        report.readiness_status = probe.status if probe else None
        report.bus.record(
            event(Kind.READINESS_PASSED, timestamp_ns=ready_ns, status=report.readiness_status)
        )


async def _calibrate(
    runtime: DockerRuntime,
    container: Container,
    report: RunReport,
    port: int,
    network_name: str,
    publish_port: bool,
) -> None:
    """Two measurements taken while the container is healthy and unsignalled.

    Both are best effort and neither produces a verdict. They run here rather
    than during shutdown for the obvious reason: each starts an auxiliary
    container, and daemon work during the window being timed would land in the
    timing.
    """
    report.pid1 = await runtime.probe_pid1(container)
    if report.config.deployment.shutdown_budget_ms > TEARDOWN_CALIBRATION_MAX_BUDGET_MS:
        report.teardown_calibration_status = "not_calibrated"
        return
    report.teardown_calibration = await runtime.measure_teardown_floor(
        port=port,
        network_name=network_name,
        publish_port=publish_port,
    )
    report.teardown_calibration_status = (
        "calibrated" if report.teardown_calibration is not None else "unavailable"
    )
    report.teardown_floor_ms = (
        report.teardown_calibration.floor_ms
        if report.teardown_calibration is not None
        else None
    )


async def _baseline(
    config: Config, runtime: DockerRuntime, container: Container, report: RunReport
) -> None:
    """Ask the service whether it works, before measuring how it dies.

    Ten sequential readiness probes establish SP002 before shutdown traffic is
    armed. This phase is also the only honest source for `sigterm_after`: the
    signal has to land inside a request, and half the measured p50 puts T0 in the
    middle of the distribution rather than in the middle of a guess.
    """
    health = config.probes.health
    report.readiness_baseline = await measure_readiness_baseline(
        host=container.host,
        port=container.host_port,
        readiness_path=config.probes.readiness.path,
        readiness_status=config.probes.readiness.expected_status,
        health_path=health.path if health is not None else None,
        health_status=health.expected_status if health is not None else None,
    )

    inflight = config.contracts.inflight
    if inflight is None:
        report.inflight_target = "disabled"
        return

    fallback = inflight.request.path is None
    report.inflight_target = "readiness_fallback" if fallback else "configured"
    report.inflight_path = inflight.request.path or config.probes.readiness.path
    if fallback:
        report.inflight_fallback_p50_ms = report.readiness_baseline.p50_ms
        report.inflight_fallback_jitter_ms = report.measurement_jitter_ms
        if report.inflight_fallback_p50_ms is not None and report.inflight_fallback_jitter_ms:
            report.inflight_fallback_ratio = (
                report.inflight_fallback_p50_ms / report.inflight_fallback_jitter_ms
            )
        if (
            report.inflight_fallback_ratio is None
            or report.inflight_fallback_ratio < MIN_JITTER_RATIO
        ):
            return

    baseline = await measure_baseline(
        host=container.host,
        port=container.host_port,
        method=inflight.request.method,
        path=report.inflight_path,
        headers=dict(inflight.request.headers),
        timeout_ms=inflight.request.expected_duration + 5000,
    )
    report.baseline = baseline
    report.bus.record(
        event(
            Kind.BASELINE_MEASURED,
            samples=baseline.samples,
            succeeded=baseline.succeeded,
            p50_ms=baseline.p50_ms,
        )
    )

    report.inflight_measurement_enabled = True
    if inflight.sigterm_after is not None:
        report.sigterm_after_ms = inflight.sigterm_after
        report.sigterm_after_source = "config"
    elif fallback:
        report.sigterm_after_ms = max(
            1, round((report.inflight_fallback_p50_ms or 0) / 2)
        )
        report.sigterm_after_source = "readiness_fallback"
    else:
        report.sigterm_after_ms = baseline.suggested_sigterm_after_ms
        report.sigterm_after_source = "baseline"


async def _shutdown(
    config: Config, runtime: DockerRuntime, container: Container, report: RunReport
) -> None:
    inflight = config.contracts.inflight
    budget_ms = config.deployment.shutdown_budget_ms
    hard_wait_ms = min(budget_ms, config.timeouts.shutdown)

    sigterm_sent = anyio.Event()
    exited = anyio.Event()
    accept_probe_armed = anyio.Event()
    inflight_request_armed = anyio.Event()
    stop_accept_probe = anyio.Event()
    report.accept_probe_interval_ms = ACCEPT_PROBE_INTERVAL_MS
    report.accept_refused_streak_target = TERMINAL_REFUSED_STREAK

    def observe(frame: DaemonEvent) -> None:
        report.daemon_events.append(frame)

    async def exit_waiter() -> None:
        # Armed before the signal: `wait` blocks on the daemon, so the response
        # lands as soon as the container dies rather than after our next poll.
        code = await runtime.wait(container, timeout_ms=hard_wait_ms + 2000)
        if code is None:
            raise DockerError(
                "Docker did not report container exit after SIGKILL; the runtime "
                "is no longer observable"
            )
        report.exit_ns = now_ns()
        report.exit_code = code
        report.bus.record(
            event(Kind.PROCESS_EXITED, timestamp_ns=report.exit_ns, exit_code=code)
        )
        exited.set()

    async def signal_sender() -> None:
        # SP004's stream must exist on both sides of T0. Do not let a very short
        # sigterm_after race the first steady-state connection attempt.
        await accept_probe_armed.wait()
        if report.inflight_measurement_enabled:
            # T0 must follow a request that has actually crossed the socket.
            # Merely scheduling the traffic tasks can otherwise make SP005
            # report `nothing_in_flight` on a perfectly measurable endpoint.
            await inflight_request_armed.wait()
        if report.inflight_measurement_enabled and report.sigterm_after_ms:
            await anyio.sleep(report.sigterm_after_ms / 1000)
        report.sigterm_ns = now_ns()
        # In a preStop rollout, routing removal owns the interval before T0.
        # Continuing to originate new connections after T0 would manufacture a
        # traffic shape that production does not have and can leave handshakes
        # in the kernel accept queue just as the listener closes.
        if config.deployment.drain.strategy is DrainStrategy.PRESTOP:
            stop_accept_probe.set()
        report.bus.record(
            event(Kind.SIGNAL_SENT, timestamp_ns=report.sigterm_ns, signal="SIGTERM")
        )
        await runtime.signal(container, "SIGTERM")
        sigterm_sent.set()

    async def accept_watcher() -> None:
        """Continuously open fresh sockets; never reuse a keep-alive connection."""
        refused_streak = 0
        refused_streak_started_ns: int | None = None
        try:
            while not exited.is_set() and not stop_accept_probe.is_set():
                cycle_started_ns = now_ns()
                # The pre-T0 sample is armed once its fresh connection attempt
                # starts. Waiting for the readiness response would add endpoint
                # latency to the requested in-flight window and can let every
                # deliberately slow request finish before SIGTERM.
                if not accept_probe_armed.is_set():
                    accept_probe_armed.set()
                attempt = await probe_new_connection(
                    host=container.host,
                    port=container.host_port,
                    path=config.probes.readiness.path,
                )
                report.accept_attempts.append(attempt)

                after_t0 = (
                    report.sigterm_ns is not None
                    and attempt.started_ns >= report.sigterm_ns
                )
                if after_t0 and attempt.outcome is AcceptOutcome.REFUSED:
                    if refused_streak == 0:
                        refused_streak_started_ns = attempt.finished_ns
                    refused_streak += 1
                    if refused_streak >= TERMINAL_REFUSED_STREAK:
                        report.accept_probe_stopped_ns = refused_streak_started_ns
                        return
                else:
                    refused_streak = 0
                    refused_streak_started_ns = None

                elapsed_ms = (now_ns() - cycle_started_ns) / 1_000_000
                remaining_ms = ACCEPT_PROBE_INTERVAL_MS - elapsed_ms
                if remaining_ms > 0:
                    await anyio.sleep(remaining_ms / 1000)
        finally:
            # Startup already established readiness, so a failure here must not
            # deadlock the experiment before SIGTERM.
            if not accept_probe_armed.is_set():
                accept_probe_armed.set()

    async def readiness_watcher() -> None:
        """Find T1: the first moment readiness stops passing after SIGTERM."""
        await sigterm_sent.wait()
        base = f"http://{container.host}:{container.host_port}"
        async with httpx.AsyncClient(base_url=base) as client:
            while not exited.is_set():
                result = await probe_http(
                    client,
                    config.probes.readiness.path,
                    expected_status=config.probes.readiness.expected_status,
                    timeout_ms=500,
                )
                if not result.ok:
                    # The dead process itself is not a readiness transition an
                    # external load balancer could have acted on before T4.
                    if exited.is_set():
                        return
                    report.readiness_drop_ns = now_ns()
                    report.readiness_drop_status = result.status
                    # A 503 means the application actively marked itself
                    # unhealthy. An unreachable endpoint means it stopped
                    # serving. Only the first is a drain signal an external load
                    # balancer could act on.
                    report.readiness_drop_mode = (
                        "status_change" if result.status is not None else "unreachable"
                    )
                    report.bus.record(
                        event(
                            Kind.READINESS_FAILED,
                            timestamp_ns=report.readiness_drop_ns,
                            status=result.status,
                            mode=report.readiness_drop_mode,
                            detail=result.error,
                        )
                    )
                    return
                await anyio.sleep(READINESS_WATCH_INTERVAL_MS / 1000)

    async def enforcer() -> None:
        """Kill only after the declared budget, and record that we had to."""
        await sigterm_sent.wait()
        with anyio.move_on_after(hard_wait_ms / 1000):
            await exited.wait()
        if not exited.is_set():
            report.sigkill_sent = True
            report.sigkill_ns = now_ns()
            report.bus.record(
                event(Kind.TIMEOUT_REACHED, what="shutdown_budget", budget_ms=hard_wait_ms)
            )
            try:
                await runtime.signal(container, "SIGKILL")
            except DockerError:
                pass

    # The event stream is opened first and torn down last. `tg.start` returns
    # only once the subscription exists, which is the point: a signal sent before
    # it would lose the `kill` frame that dates the whole measurement.
    async with anyio.create_task_group() as watcher:
        await watcher.start(runtime.watch_events, container.id, _WATCHED_ACTIONS, observe)

        async with anyio.create_task_group() as tg:
            tg.start_soon(exit_waiter)
            tg.start_soon(accept_watcher)
            tg.start_soon(signal_sender)
            tg.start_soon(readiness_watcher)
            tg.start_soon(enforcer)

            if inflight is not None and report.inflight_measurement_enabled:
                request_timeout = (
                    inflight.request.expected_duration + budget_ms + 5000
                )
                report.requests = await run_long_requests(
                    bus=report.bus,
                    host=container.host,
                    port=container.host_port,
                    method=inflight.request.method,
                    path=report.inflight_path or config.probes.readiness.path,
                    headers=dict(inflight.request.headers),
                    concurrent=inflight.concurrent,
                    timeout_ms=request_timeout,
                    request_sent_event=inflight_request_armed,
                )
            await exited.wait()

        # The exit is known; the frame that dates it is still in flight. It
        # travels the same socket as everything else, so it arrives a millisecond
        # or two after `wait` returns.
        with anyio.move_on_after(_DIE_FRAME_GRACE_MS / 1000):
            while not any(frame.action == "die" for frame in report.daemon_events):
                await anyio.sleep(0.005)
        watcher.cancel_scope.cancel()
