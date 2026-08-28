"""Run-scoped traffic probe entry point.

This file is copied into a generic Python image at run time. It deliberately
contains no Docker client and receives no host mount. The host owns container
lifecycle; this process owns every application-facing socket and timestamp.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import anyio

from rolloutkit.engine.bus import EventBus
from rolloutkit.traffic.accept_probe import (
    ACCEPT_PROBE_INTERVAL_MS,
    TERMINAL_REFUSED_STREAK,
    AcceptAttempt,
    AcceptOutcome,
    probe_new_connection,
)
from rolloutkit.traffic.client import (
    Outcome,
    RequestResult,
    perform_request,
    verify_keep_alive,
)
from rolloutkit.traffic.generator import run_long_requests

CONTROL_PORT = 8765
READINESS_WATCH_INTERVAL_MS = 20


def _http_probe(host: str, port: int, path: str, expected: int) -> dict[str, Any]:
    started = time.monotonic_ns()
    connection = HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(256)
        return {
            "ok": response.status == expected,
            "status": response.status,
            "latency_ns": time.monotonic_ns() - started,
            "headers": dict(response.getheaders()),
            "body_head": body.decode("utf-8", "replace"),
            "body_head_bytes": len(body),
            "error": None,
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": None,
            "latency_ns": time.monotonic_ns() - started,
            "headers": {},
            "body_head": "",
            "body_head_bytes": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        connection.close()


def _wait_until_unix_ns(deadline_ns: int) -> None:
    while True:
        remaining = deadline_ns - time.time_ns()
        if remaining <= 0:
            return
        time.sleep(min(remaining / 1_000_000_000, 0.01))


def _request_dict(result: RequestResult, t0_ns: int) -> dict[str, Any]:
    def offset(value: int | None) -> float | None:
        return None if value is None else (value - t0_ns) / 1_000_000

    return {
        "request_id": result.request_id,
        "outcome": str(result.outcome),
        "phase": str(result.phase),
        "status": result.status,
        "headers": result.headers,
        "body_bytes": result.body_bytes,
        "expected_body_bytes": result.expected_body_bytes,
        "started_offset_ms": offset(result.started_ns),
        "connected_offset_ms": offset(result.connected_ns),
        "request_sent_offset_ms": offset(result.request_sent_ns),
        "first_byte_offset_ms": offset(result.first_byte_ns),
        "finished_offset_ms": offset(result.finished_ns),
        "error_detail": result.error_detail,
    }


def _attempt_dict(attempt: AcceptAttempt, t0_ns: int) -> dict[str, Any]:
    def offset(value: int | None) -> float | None:
        return None if value is None else (value - t0_ns) / 1_000_000

    return {
        "started_offset_ms": offset(attempt.started_ns),
        "connected_offset_ms": offset(attempt.connected_ns),
        "finished_offset_ms": offset(attempt.finished_ns),
        "outcome": str(attempt.outcome),
        "error": attempt.error,
    }


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.shutdown_result: dict[str, Any] | None = None
        self.shutdown_error: str | None = None
        self.shutdown_armed = threading.Event()
        self.t0_unix_ns: int | None = None
        self.calibration_result: dict[str, Any] | None = None
        self.calibration_armed = threading.Event()


STATE = State()


async def _measure_baseline(payload: dict[str, Any]) -> dict[str, Any]:
    samples = int(payload.get("samples", 25))
    limiter = anyio.CapacityLimiter(25)
    results: list[RequestResult | None] = [None] * samples

    async def one(index: int) -> None:
        async with limiter:
            results[index] = await perform_request(
                request_id=-(index + 1),
                host=str(payload["host"]),
                port=int(payload["port"]),
                method=str(payload["method"]),
                path=str(payload["path"]),
                headers=dict(payload["headers"]),
                timeout_ms=int(payload["timeout_ms"]),
            )

    async with anyio.create_task_group() as group:
        for index in range(samples):
            group.start_soon(one, index)
    completed = [item for item in results if item is not None]
    durations = [
        (item.finished_ns - item.started_ns) / 1_000_000
        for item in completed
        if item.finished_ns is not None
    ]
    statuses: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    succeeded = 0
    complete_count = 0
    for item in completed:
        outcome = str(item.outcome)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if item.outcome is Outcome.COMPLETED:
            complete_count += 1
        if item.status is not None:
            key = str(item.status)
            statuses[key] = statuses.get(key, 0) + 1
            if 200 <= item.status < 300 and item.outcome is Outcome.COMPLETED:
                succeeded += 1
    keep_alive = await verify_keep_alive(
        host=str(payload["host"]),
        port=int(payload["port"]),
        method=str(payload["method"]),
        path=str(payload["path"]),
        headers=dict(payload["headers"]),
        timeout_ms=int(payload["timeout_ms"]),
    )
    return {
        "samples": len(completed),
        "completed": complete_count,
        "succeeded": succeeded,
        "statuses": statuses,
        "outcomes": outcomes,
        "durations_ms": durations,
        "keep_alive_established": keep_alive,
    }


async def _shutdown_job(payload: dict[str, Any]) -> None:
    host = str(payload["host"])
    port = int(payload["port"])
    readiness_path = str(payload["readiness_path"])
    readiness_status = int(payload["readiness_status"])
    inflight = payload.get("inflight")
    prestop = bool(payload.get("prestop"))
    t0_unix_ns = int(payload["t0_unix_ns"])
    inflight_lead_ms = int(payload.get("inflight_lead_ms") or 0)
    hard_wait_ms = int(payload["hard_wait_ms"])

    attempts: list[AcceptAttempt] = []
    requests: list[RequestResult] = []
    accept_armed = anyio.Event()
    request_armed = anyio.Event()
    t0_reached = anyio.Event()
    stop = anyio.Event()
    stop_accept = anyio.Event()
    readiness_drop: dict[str, Any] | None = None
    accept_stopped_offset_ms: float | None = None
    t0_monotonic_ns = 0

    async def accept_watcher() -> None:
        nonlocal accept_stopped_offset_ms
        refused = 0
        refused_started_ns: int | None = None
        while not stop.is_set() and not stop_accept.is_set():
            cycle = time.monotonic_ns()
            accept_armed.set()
            attempt = await probe_new_connection(
                host=host, port=port, path=readiness_path
            )
            attempts.append(attempt)
            if t0_reached.is_set() and attempt.outcome is AcceptOutcome.REFUSED:
                if refused == 0:
                    refused_started_ns = attempt.finished_ns
                refused += 1
                if refused >= TERMINAL_REFUSED_STREAK:
                    if refused_started_ns is not None:
                        accept_stopped_offset_ms = (
                            refused_started_ns - t0_monotonic_ns
                        ) / 1_000_000
                    return
            else:
                refused = 0
                refused_started_ns = None
            elapsed = (time.monotonic_ns() - cycle) / 1_000_000
            if elapsed < ACCEPT_PROBE_INTERVAL_MS:
                await anyio.sleep((ACCEPT_PROBE_INTERVAL_MS - elapsed) / 1000)

    async def inflight_traffic() -> None:
        nonlocal requests
        if not inflight:
            request_armed.set()
            return
        launch_unix_ns = t0_unix_ns - inflight_lead_ms * 1_000_000
        await anyio.to_thread.run_sync(_wait_until_unix_ns, launch_unix_ns)
        requests = await run_long_requests(
            bus=EventBus(),
            host=host,
            port=port,
            method=str(inflight["method"]),
            path=str(inflight["path"]),
            headers=dict(inflight["headers"]),
            concurrent=int(inflight["concurrent"]),
            timeout_ms=int(inflight["timeout_ms"]),
            request_sent_event=request_armed,
        )

    async def clock() -> None:
        nonlocal t0_monotonic_ns
        await accept_armed.wait()
        await request_armed.wait()
        STATE.shutdown_armed.set()
        await anyio.to_thread.run_sync(_wait_until_unix_ns, t0_unix_ns)
        t0_monotonic_ns = time.monotonic_ns()
        t0_reached.set()
        if prestop:
            stop_accept.set()

    async def readiness_watcher() -> None:
        nonlocal readiness_drop
        await t0_reached.wait()
        deadline = time.monotonic_ns() + (hard_wait_ms + 2000) * 1_000_000
        while time.monotonic_ns() < deadline:
            result = await anyio.to_thread.run_sync(
                _http_probe, host, port, readiness_path, readiness_status
            )
            if not result["ok"]:
                observed_ns = time.monotonic_ns()
                readiness_drop = {
                    "offset_ms": (observed_ns - t0_monotonic_ns) / 1_000_000,
                    "status": result["status"],
                    "mode": "status_change"
                    if result["status"] is not None
                    else "unreachable",
                    "error": result["error"],
                }
                return
            await anyio.sleep(READINESS_WATCH_INTERVAL_MS / 1000)

    async def deadline() -> None:
        await t0_reached.wait()
        await anyio.sleep((hard_wait_ms + 2500) / 1000)
        stop.set()

    async with anyio.create_task_group() as group:
        group.start_soon(accept_watcher)
        group.start_soon(inflight_traffic)
        group.start_soon(clock)
        group.start_soon(readiness_watcher)
        group.start_soon(deadline)
        await t0_reached.wait()
        while not stop.is_set():
            accept_done = prestop or accept_stopped_offset_ms is not None
            readiness_done = readiness_drop is not None
            inflight_done = not inflight or bool(requests)
            if accept_done and readiness_done and inflight_done:
                stop.set()
                break
            await anyio.sleep(0.01)
        group.cancel_scope.cancel()

    STATE.shutdown_result = {
        "t0_unix_ns": t0_unix_ns,
        "attempts": [_attempt_dict(item, t0_monotonic_ns) for item in attempts],
        "requests": [_request_dict(item, t0_monotonic_ns) for item in requests],
        "readiness_drop": readiness_drop,
        "accept_stopped_offset_ms": accept_stopped_offset_ms,
    }


def _run_shutdown(payload: dict[str, Any]) -> None:
    try:
        anyio.run(_shutdown_job, payload)
    except BaseException as exc:  # noqa: BLE001 - returned to the host as evidence
        STATE.shutdown_error = f"{type(exc).__name__}: {exc}"
        STATE.shutdown_armed.set()


async def _calibration_job(payload: dict[str, Any]) -> None:
    host = str(payload["host"])
    port = int(payload["port"])
    t0_unix_ns = int(payload["t0_unix_ns"])
    attempts: list[AcceptAttempt] = []

    # Docker returning from ``start`` does not mean the auxiliary Python HTTP
    # server has bound its socket yet.  Arming the host-side SIGKILL before one
    # successful bridge connection made a slow Linux runner report a false
    # zero-millisecond floor: every post-T0 attempt was already refused.  Prove
    # the listener is reachable from this namespace before announcing that the
    # calibration is armed.
    ready_deadline = time.monotonic() + 5
    while True:
        ready = await probe_new_connection(host=host, port=port, path="/")
        if ready.connected_ns is not None:
            break
        if time.monotonic() >= ready_deadline:
            raise TimeoutError("calibration listener did not become reachable")
        await anyio.sleep(0.01)
    STATE.calibration_armed.set()
    await anyio.to_thread.run_sync(_wait_until_unix_ns, t0_unix_ns)
    t0_ns = time.monotonic_ns()
    refused = 0
    while refused < TERMINAL_REFUSED_STREAK:
        attempt = await probe_new_connection(host=host, port=port, path="/")
        attempts.append(attempt)
        refused = refused + 1 if attempt.outcome is AcceptOutcome.REFUSED else 0
        await anyio.sleep(0.005)
    accepted = [item.connected_ns for item in attempts if item.connected_ns is not None]
    STATE.calibration_result = {
        "floor_ms": 0.0 if not accepted else (max(accepted) - t0_ns) / 1_000_000,
        "attempts": [_attempt_dict(item, t0_ns) for item in attempts],
    }


def _run_calibration(payload: dict[str, Any]) -> None:
    try:
        anyio.run(_calibration_job, payload)
    except BaseException as exc:  # noqa: BLE001
        STATE.calibration_result = {"error": f"{type(exc).__name__}: {exc}"}
        STATE.calibration_armed.set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return

    def _read(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "unix_ns": time.time_ns()})
        elif self.path == "/shutdown/result":
            if STATE.shutdown_error:
                self._send(500, {"error": STATE.shutdown_error})
            elif STATE.shutdown_result is None:
                self._send(202, {"pending": True})
            else:
                self._send(200, STATE.shutdown_result)
        elif self.path == "/calibration/result":
            if STATE.calibration_result is None:
                self._send(202, {"pending": True})
            else:
                self._send(200, STATE.calibration_result)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read()
        if self.path == "/startup":
            host, port = str(payload["host"]), int(payload["port"])
            started_unix_ns = int(payload["target_started_unix_ns"])
            deadline = time.time_ns() + int(payload["budget_ms"]) * 1_000_000
            tcp_unix_ns: int | None = None
            last: dict[str, Any] | None = None
            while time.time_ns() < deadline:
                if tcp_unix_ns is None:
                    try:
                        connection = socket.create_connection((host, port), timeout=0.5)
                        connection.close()
                        tcp_unix_ns = time.time_ns()
                    except OSError:
                        pass
                last = _http_probe(
                    host, port, str(payload["path"]), int(payload["expected_status"])
                )
                if last["ok"]:
                    ready_unix_ns = time.time_ns()
                    self._send(
                        200,
                        {
                            "tcp_open_ms": None
                            if tcp_unix_ns is None
                            else (tcp_unix_ns - started_unix_ns) / 1_000_000,
                            "readiness_ms": (ready_unix_ns - started_unix_ns)
                            / 1_000_000,
                            "status": last["status"],
                        },
                    )
                    return
                time.sleep(0.05)
            self._send(408, {"last": last})
        elif self.path == "/baseline":
            samples = [
                _http_probe(
                    str(payload["host"]),
                    int(payload["port"]),
                    str(payload["readiness_path"]),
                    int(payload["readiness_status"]),
                )
                for _ in range(10)
            ]
            health = None
            if payload.get("health_path") is not None:
                health = _http_probe(
                    str(payload["host"]),
                    int(payload["port"]),
                    str(payload["health_path"]),
                    int(payload["health_status"]),
                )
            baseline = None
            if payload.get("inflight"):
                item = payload["inflight"]
                baseline = anyio.run(_measure_baseline, item)
            self._send(200, {"readiness": samples, "health": health, "baseline": baseline})
        elif self.path == "/jitter":
            values: list[int] = []
            for _ in range(int(payload.get("samples", 5))):
                started = time.monotonic_ns()
                connection = socket.create_connection(
                    (str(payload["host"]), int(payload["port"])), timeout=1
                )
                connection.close()
                values.append(time.monotonic_ns() - started)
            self._send(200, {"samples_ns": values})
        elif self.path == "/shutdown/start":
            STATE.shutdown_result = None
            STATE.shutdown_error = None
            STATE.shutdown_armed.clear()
            STATE.t0_unix_ns = int(payload["t0_unix_ns"])
            threading.Thread(target=_run_shutdown, args=(payload,), daemon=True).start()
            if not STATE.shutdown_armed.wait(10):
                self._send(500, {"error": "shutdown probe did not arm"})
            else:
                self._send(200, {"armed": True, "t0_unix_ns": STATE.t0_unix_ns})
        elif self.path == "/calibration/start":
            STATE.calibration_result = None
            STATE.calibration_armed.clear()
            threading.Thread(target=_run_calibration, args=(payload,), daemon=True).start()
            if not STATE.calibration_armed.wait(10):
                self._send(500, {"error": "calibration probe did not arm"})
            else:
                self._send(200, {"armed": True})
        else:
            self._send(404, {"error": "not found"})


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
