"""Host-side controller for the run-scoped traffic probe."""

from __future__ import annotations

import time
from typing import Any

import anyio
import httpx

from preflightkit.engine.context import RunReport
from preflightkit.engine.events import RequestPhase, now_ns
from preflightkit.probes.http import ProbeResult
from preflightkit.runtime.base import Container, ContainerSpec, TeardownCalibration
from preflightkit.runtime.docker import DockerError, DockerRuntime
from preflightkit.traffic.accept_probe import AcceptAttempt, AcceptOutcome
from preflightkit.traffic.baseline import Baseline, ReadinessBaseline
from preflightkit.traffic.client import Outcome, RequestResult


class TrafficProbe:
    def __init__(self, runtime: DockerRuntime, container: Container) -> None:
        self.runtime = runtime
        self.container = container
        self.client = httpx.AsyncClient(
            base_url=f"http://{container.host}:{container.host_port}",
            timeout=httpx.Timeout(30.0),
        )

    @classmethod
    async def start(
        cls,
        runtime: DockerRuntime,
        *,
        image: str,
        network_name: str,
        name: str,
    ) -> TrafficProbe:
        container = await runtime.start_traffic_probe(
            image=image, network_name=network_name, name=name
        )
        probe = cls(runtime, container)
        try:
            with anyio.fail_after(15):
                while True:
                    try:
                        response = await probe.client.get("/health", timeout=1)
                        if response.status_code == 200:
                            return probe
                    except httpx.HTTPError:
                        pass
                    await anyio.sleep(0.05)
        except BaseException:
            logs = await runtime.logs(container, tail=100)
            await probe.close(remove=True)
            detail = logs.strip()[-2000:] or "no probe logs"
            raise DockerError(f"traffic probe did not become ready: {detail}") from None

    async def close(self, *, remove: bool = False) -> None:
        await self.client.aclose()
        if remove:
            await self.runtime.remove(self.container)

    async def _post(
        self, path: str, payload: dict[str, Any], *, timeout: float = 30
    ) -> dict[str, Any]:
        response = await self.client.post(path, json=payload, timeout=timeout)
        if response.status_code >= 400:
            raise DockerError(
                f"traffic probe {path} failed: HTTP {response.status_code}: "
                f"{response.text[-1000:]}"
            )
        return response.json()

    async def startup(
        self,
        *,
        target_started_unix_ns: int,
        port: int,
        path: str,
        expected_status: int,
        budget_ms: int,
    ) -> dict[str, Any]:
        return await self._post(
            "/startup",
            {
                "host": "target",
                "port": port,
                "path": path,
                "expected_status": expected_status,
                "budget_ms": budget_ms,
                "target_started_unix_ns": target_started_unix_ns,
            },
            timeout=budget_ms / 1000 + 5,
        )

    async def jitter(self, port: int) -> list[int]:
        result = await self._post(
            "/jitter", {"host": "target", "port": port, "samples": 5}
        )
        return [int(value) for value in result["samples_ns"]]

    async def baseline(self, report: RunReport) -> None:
        config = report.config
        inflight = config.contracts.inflight
        report.inflight_target = (
            "disabled"
            if inflight is None
            else "readiness_fallback"
            if inflight.request.path is None
            else "configured"
        )
        report.inflight_path = (
            None
            if inflight is None
            else inflight.request.path or config.probes.readiness.path
        )
        item = None
        if inflight is not None:
            item = {
                "host": "target",
                "port": config.target.port,
                "method": inflight.request.method,
                "path": report.inflight_path,
                "headers": dict(inflight.request.headers),
                "timeout_ms": inflight.request.expected_duration + 5000,
            }
        health = config.probes.health
        result = await self._post(
            "/baseline",
            {
                "host": "target",
                "port": config.target.port,
                "readiness_path": config.probes.readiness.path,
                "readiness_status": config.probes.readiness.expected_status,
                "health_path": health.path if health else None,
                "health_status": health.expected_status if health else None,
                "inflight": item,
            },
            timeout=(inflight.request.expected_duration / 1000 + 15)
            if inflight
            else 15,
        )
        report.readiness_baseline = ReadinessBaseline(
            samples=[_probe_result(value) for value in result["readiness"]],
            health_sample=_probe_result(result["health"])
            if result.get("health")
            else None,
        )
        if inflight is None:
            return
        if result.get("baseline") is not None:
            raw = result["baseline"]
            report.baseline = Baseline(
                samples=int(raw["samples"]),
                completed=int(raw["completed"]),
                succeeded=int(raw["succeeded"]),
                statuses={str(k): int(v) for k, v in raw["statuses"].items()},
                outcomes={str(k): int(v) for k, v in raw["outcomes"].items()},
                durations_ms=[float(value) for value in raw["durations_ms"]],
                keep_alive_established=raw["keep_alive_established"],
            )

    async def shutdown(
        self,
        runtime: DockerRuntime,
        container: Container,
        report: RunReport,
    ) -> None:
        config = report.config
        inflight = config.contracts.inflight
        hard_wait_ms = min(
            config.deployment.shutdown_budget_ms, config.timeouts.shutdown
        )
        report.accept_probe_interval_ms = 50
        report.accept_refused_streak_target = 3
        lead_ms = report.sigterm_after_ms if report.inflight_measurement_enabled else 0
        t0_unix_ns = time.time_ns() + (lead_ms + 300) * 1_000_000
        inflight_payload = None
        if inflight is not None and report.inflight_measurement_enabled:
            inflight_payload = {
                "method": inflight.request.method,
                "path": report.inflight_path or config.probes.readiness.path,
                "headers": dict(inflight.request.headers),
                "concurrent": inflight.concurrent,
                "timeout_ms": inflight.request.expected_duration
                + config.deployment.shutdown_budget_ms
                + 5000,
            }
        await self._post(
            "/shutdown/start",
            {
                "host": "target",
                "port": config.target.port,
                "readiness_path": config.probes.readiness.path,
                "readiness_status": config.probes.readiness.expected_status,
                "prestop": str(config.deployment.drain.strategy) == "prestop",
                "inflight": inflight_payload,
                "inflight_lead_ms": lead_ms,
                "hard_wait_ms": hard_wait_ms,
                "t0_unix_ns": t0_unix_ns,
            },
            timeout=15,
        )

        frames = []

        def observe(frame) -> None:
            frames.append(frame)
            report.daemon_events.append(frame)

        async def exit_waiter() -> None:
            code = await runtime.wait(container, timeout_ms=hard_wait_ms + 2000)
            if code is None:
                raise DockerError("Docker did not report target exit after SIGKILL")
            report.exit_ns = now_ns()
            report.exit_code = code

        async def signal_and_enforce() -> None:
            await anyio.to_thread.run_sync(_wait_until_unix_ns, t0_unix_ns)
            report.sigterm_ns = now_ns()
            report.probe_clock_alignment_ms = (
                time.time_ns() - t0_unix_ns
            ) / 1_000_000
            await runtime.signal(container, "SIGTERM")
            with anyio.move_on_after(hard_wait_ms / 1000):
                while report.exit_ns is None:
                    await anyio.sleep(0.005)
            if report.exit_ns is None:
                report.sigkill_sent = True
                report.sigkill_ns = now_ns()
                try:
                    await runtime.signal(container, "SIGKILL")
                except DockerError:
                    pass

        async with anyio.create_task_group() as watcher:
            await watcher.start(
                runtime.watch_events, container.id, ("kill", "die"), observe
            )
            async with anyio.create_task_group() as group:
                group.start_soon(exit_waiter)
                group.start_soon(signal_and_enforce)
            with anyio.move_on_after(0.5):
                while not any(frame.action == "die" for frame in frames):
                    await anyio.sleep(0.005)
            watcher.cancel_scope.cancel()

        result = await self._poll_result(
            "/shutdown/result", timeout_ms=hard_wait_ms + 5000
        )
        origin = report.sigterm_ns or now_ns()
        report.accept_attempts = [
            _accept_attempt(value, origin) for value in result["attempts"]
        ]
        report.requests = [_request_result(value, origin) for value in result["requests"]]
        drop = result.get("readiness_drop")
        if drop:
            report.readiness_drop_ns = origin + int(drop["offset_ms"] * 1_000_000)
            report.readiness_drop_status = drop["status"]
            report.readiness_drop_mode = drop["mode"]
        stopped = result.get("accept_stopped_offset_ms")
        if stopped is not None:
            stopped_ns = origin + int(stopped * 1_000_000)
            if report.sigkill_ns is None or stopped_ns < report.sigkill_ns:
                report.accept_probe_stopped_ns = stopped_ns

    async def measure_teardown_floor(
        self, *, network_name: str, image: str, samples: int = 5
    ) -> TeardownCalibration | None:
        values: list[float] = []
        for index in range(samples):
            alias = f"pfk-floor-{index}-{time.time_ns()}"
            auxiliary: Container | None = None
            try:
                auxiliary = await self.runtime.start(
                    ContainerSpec(
                        image=image,
                        port=8899,
                        command=["python", "-B", "-m", "http.server", "8899"],
                        name=alias,
                        network_name=network_name,
                        network_aliases=(alias,),
                        memory_bytes=128 * 1024 * 1024,
                        nano_cpus=500_000_000,
                    )
                )
                await anyio.sleep(0.1)
                t0 = time.time_ns() + 300_000_000
                await self._post(
                    "/calibration/start",
                    {"host": alias, "port": 8899, "t0_unix_ns": t0},
                )
                await anyio.to_thread.run_sync(_wait_until_unix_ns, t0)
                await self.runtime.signal(auxiliary, "SIGKILL")
                await self.runtime.wait(auxiliary, timeout_ms=5000)
                result = await self._poll_result("/calibration/result", timeout_ms=5000)
                if result.get("error"):
                    return None
                values.append(float(result["floor_ms"]))
            except DockerError:
                return None
            finally:
                if auxiliary is not None:
                    await self.runtime.remove(auxiliary)
        return TeardownCalibration(tuple(values))

    async def _poll_result(self, path: str, *, timeout_ms: int) -> dict[str, Any]:
        with anyio.fail_after(timeout_ms / 1000):
            while True:
                response = await self.client.get(path, timeout=5)
                if response.status_code == 200:
                    return response.json()
                if response.status_code >= 400:
                    raise DockerError(f"traffic probe {path}: {response.text}")
                await anyio.sleep(0.02)
        raise DockerError(f"traffic probe {path} timed out")


def _wait_until_unix_ns(deadline_ns: int) -> None:
    while True:
        remaining = deadline_ns - time.time_ns()
        if remaining <= 0:
            return
        time.sleep(min(remaining / 1_000_000_000, 0.01))


def _probe_result(raw: dict[str, Any]) -> ProbeResult:
    return ProbeResult(
        ok=bool(raw["ok"]),
        status=raw["status"],
        latency_ns=int(raw["latency_ns"]),
        headers=dict(raw.get("headers") or {}),
        body_head=str(raw.get("body_head") or ""),
        body_head_bytes=int(raw.get("body_head_bytes") or 0),
        error=raw.get("error"),
    )


def _at(origin: int, value: float | None) -> int | None:
    return None if value is None else origin + int(value * 1_000_000)


def _accept_attempt(raw: dict[str, Any], origin: int) -> AcceptAttempt:
    return AcceptAttempt(
        started_ns=_at(origin, raw["started_offset_ms"]) or origin,
        connected_ns=_at(origin, raw.get("connected_offset_ms")),
        finished_ns=_at(origin, raw["finished_offset_ms"]) or origin,
        outcome=AcceptOutcome(raw["outcome"]),
        error=raw.get("error"),
    )


def _request_result(raw: dict[str, Any], origin: int) -> RequestResult:
    return RequestResult(
        request_id=int(raw["request_id"]),
        outcome=Outcome(raw["outcome"]),
        phase=RequestPhase(raw["phase"]),
        status=raw.get("status"),
        headers=dict(raw.get("headers") or {}),
        body_bytes=int(raw.get("body_bytes") or 0),
        expected_body_bytes=raw.get("expected_body_bytes"),
        started_ns=_at(origin, raw["started_offset_ms"]) or origin,
        connected_ns=_at(origin, raw.get("connected_offset_ms")),
        request_sent_ns=_at(origin, raw.get("request_sent_offset_ms")),
        first_byte_ns=_at(origin, raw.get("first_byte_offset_ms")),
        finished_ns=_at(origin, raw.get("finished_offset_ms")),
        error_detail=raw.get("error_detail"),
    )
