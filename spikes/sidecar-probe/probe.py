#!/usr/bin/env python3
"""Raw lifecycle probe used only by the sidecar placement spike.

This process deliberately imports the product traffic clients and no contracts.
It creates and signals a target through the mounted Docker socket, originates
TCP traffic from its own network namespace, and emits one JSON document.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import statistics
import sys
import time
import urllib.parse
from typing import Any

import anyio

from preflightkit.engine.bus import EventBus
from preflightkit.traffic.accept_probe import (
    ACCEPT_PROBE_INTERVAL_MS,
    TERMINAL_REFUSED_STREAK,
    AcceptAttempt,
    AcceptOutcome,
    probe_new_connection,
)
from preflightkit.traffic.client import BROKEN_OUTCOMES, Outcome, RequestResult
from preflightkit.traffic.generator import run_long_requests


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=30)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class DockerAPI:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        version = self.request("GET", "/version")
        self.prefix = f"/v{version['ApiVersion']}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
        versioned: bool = False,
    ) -> Any:
        connection = UnixHTTPConnection(self.socket_path)
        encoded = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if encoded else {}
        target = f"{self.prefix}{path}" if versioned else path
        connection.request(method, target, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        if response.status not in expected:
            detail = payload.decode(errors="replace")[-1000:]
            raise RuntimeError(f"Docker {method} {path}: {response.status}: {detail}")
        if not payload:
            return None
        content_type = response.getheader("Content-Type", "")
        if "/events?" not in path and (
            "json" in content_type or payload[:1] in (b"{", b"[")
        ):
            return json.loads(payload)
        return payload.decode(errors="replace")

    def ping_ms(self) -> float:
        started = time.monotonic_ns()
        self.request("GET", "/_ping", expected=(200,))
        return (time.monotonic_ns() - started) / 1_000_000

    def create_target(
        self,
        *,
        image: str,
        env: dict[str, str],
        command: list[str] | None,
        network: str,
        name: str,
    ) -> str:
        payload: dict[str, Any] = {
            "Image": image,
            "Env": [f"{key}={value}" for key, value in env.items()],
            "HostConfig": {"NetworkMode": network},
            "NetworkingConfig": {
                "EndpointsConfig": {network: {"Aliases": ["target"]}}
            },
        }
        if command:
            payload["Cmd"] = command
        result = self.request(
            "POST",
            f"/containers/create?name={urllib.parse.quote(name)}",
            body=payload,
            versioned=True,
        )
        return result["Id"]

    def start(self, container_id: str) -> None:
        self.request(
            "POST",
            f"/containers/{container_id}/start",
            expected=(204, 304),
            versioned=True,
        )

    def inspect(self, container_id: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/containers/{container_id}/json", versioned=True
        )

    def signal(self, container_id: str, signal: str) -> None:
        self.request(
            "POST",
            f"/containers/{container_id}/kill?signal={signal}",
            expected=(204, 409),
            versioned=True,
        )

    def wait(self, container_id: str) -> int:
        result = self.request(
            "POST",
            f"/containers/{container_id}/wait?condition=not-running",
            versioned=True,
        )
        return int(result["StatusCode"])

    def remove(self, container_id: str) -> None:
        self.request(
            "DELETE",
            f"/containers/{container_id}?force=1&v=1",
            expected=(204, 404),
            versioned=True,
        )

    def teardown_sample_ms(self, *, network: str, name: str) -> float:
        created_at = int(time.time()) - 1
        container_id = self.create_target(
            image="busybox:latest",
            env={},
            command=["sleep", "30"],
            network=network,
            name=name,
        )
        try:
            self.start(container_id)
            self.signal(container_id, "SIGKILL")
            self.wait(container_id)
            until = int(time.time()) + 1
            filters = json.dumps(
                {
                    "container": [container_id],
                    "event": ["kill", "die"],
                    "type": ["container"],
                },
                separators=(",", ":"),
            )
            path = (
                "/events?since="
                f"{created_at}&until={until}&filters={urllib.parse.quote(filters)}"
            )
            payload = self.request("GET", path, versioned=True)
            events = [json.loads(line) for line in str(payload).splitlines() if line]
            stamps = {event["Action"]: int(event["timeNano"]) for event in events}
            return (stamps["die"] - stamps["kill"]) / 1_000_000
        finally:
            self.remove(container_id)


def _http_status(host: str, port: int, path: str, timeout: float = 0.5) -> int | None:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        response.read()
        return response.status
    except OSError:
        return None
    finally:
        connection.close()


async def _wait_for_target(
    host: str, port: int, ready_path: str, started_ns: int
) -> tuple[float, float]:
    tcp_open_ns: int | None = None
    while True:
        if tcp_open_ns is None:
            try:
                stream = await anyio.connect_tcp(host, port)
            except OSError:
                pass
            else:
                tcp_open_ns = time.monotonic_ns()
                await stream.aclose()
        status = await anyio.to_thread.run_sync(_http_status, host, port, ready_path)
        if status == 200:
            ready_ns = time.monotonic_ns()
            break
        await anyio.sleep(0.05)
    assert tcp_open_ns is not None
    return (
        (tcp_open_ns - started_ns) / 1_000_000,
        (ready_ns - started_ns) / 1_000_000,
    )


def _attempt_dict(attempt: AcceptAttempt, sigterm_ns: int) -> dict[str, Any]:
    return {
        "started_offset_ms": (attempt.started_ns - sigterm_ns) / 1_000_000,
        "connected_offset_ms": (
            None
            if attempt.connected_ns is None
            else (attempt.connected_ns - sigterm_ns) / 1_000_000
        ),
        "finished_offset_ms": (attempt.finished_ns - sigterm_ns) / 1_000_000,
        "outcome": str(attempt.outcome),
        "error": attempt.error,
    }


def _inflight_evidence(
    requests: list[RequestResult], sigterm_ns: int
) -> dict[str, Any]:
    in_flight = [
        result
        for result in requests
        if result.connected_ns is not None
        and result.connected_ns <= sigterm_ns
        and (result.finished_ns is None or result.finished_ns > sigterm_ns)
    ]
    completed = [result for result in in_flight if result.outcome is Outcome.COMPLETED]
    broken = [result for result in in_flight if result.outcome in BROKEN_OUTCOMES]
    return {
        "issued": len(requests),
        "in_flight_at_sigterm": len(in_flight),
        "completed": len(completed),
        "broken": len(broken),
        "completion_rate": (
            None if not in_flight else round(len(completed) / len(in_flight), 4)
        ),
        "outcomes": {
            str(outcome): sum(1 for result in in_flight if result.outcome is outcome)
            for outcome in Outcome
        },
    }


async def _measure(args: argparse.Namespace) -> dict[str, Any]:
    process_started_ns = time.time_ns()
    docker = await anyio.to_thread.run_sync(DockerAPI, args.docker_socket)
    jitter_samples = [
        await anyio.to_thread.run_sync(docker.ping_ms) for _ in range(args.jitter_samples)
    ]
    floor_samples = [
        await anyio.to_thread.run_sync(
            lambda index=index: docker.teardown_sample_ms(
                network=args.network,
                name=f"{args.name}-floor-{index}",
            )
        )
        for index in range(args.floor_samples)
    ]

    target_id = await anyio.to_thread.run_sync(
        lambda: docker.create_target(
            image=args.target_image,
            env=json.loads(args.target_env_json),
            command=json.loads(args.target_command_json)
            if args.target_command_json
            else None,
            network=args.network,
            name=f"{args.name}-target",
        )
    )
    try:
        target_start_requested_ns = time.monotonic_ns()
        await anyio.to_thread.run_sync(docker.start, target_id)
        target_started_ns = time.monotonic_ns()
        inspected = await anyio.to_thread.run_sync(docker.inspect, target_id)
        target_host = (
            inspected["NetworkSettings"]["Networks"][args.network]["IPAddress"]
            if args.probe_location == "host"
            else "target"
        )
        tcp_open_ms, readiness_ms = await _wait_for_target(
            target_host, args.target_port, args.ready_path, target_started_ns
        )

        # Keep the same ten-sample pre-signal readiness phase as the product.
        readiness_latencies: list[float] = []
        for _ in range(10):
            started = time.monotonic_ns()
            status = await anyio.to_thread.run_sync(
                _http_status, target_host, args.target_port, args.ready_path
            )
            if status != 200:
                raise RuntimeError(f"readiness baseline returned {status}")
            readiness_latencies.append((time.monotonic_ns() - started) / 1_000_000)

        accept_attempts: list[AcceptAttempt] = []
        requests: list[RequestResult] = []
        accept_armed = anyio.Event()
        request_armed = anyio.Event()
        signal_sent = anyio.Event()
        exited = anyio.Event()
        sigterm_ns = 0
        exit_code: int | None = None

        async def accept_watcher() -> None:
            refused = 0
            while not exited.is_set():
                cycle_started = time.monotonic_ns()
                accept_armed.set()
                attempt = await probe_new_connection(
                    host=target_host,
                    port=args.target_port,
                    path=args.ready_path,
                )
                accept_attempts.append(attempt)
                refused = refused + 1 if attempt.outcome is AcceptOutcome.REFUSED else 0
                if signal_sent.is_set() and refused >= TERMINAL_REFUSED_STREAK:
                    return
                elapsed_ms = (time.monotonic_ns() - cycle_started) / 1_000_000
                if elapsed_ms < ACCEPT_PROBE_INTERVAL_MS:
                    await anyio.sleep((ACCEPT_PROBE_INTERVAL_MS - elapsed_ms) / 1000)

        async def inflight_traffic() -> None:
            nonlocal requests
            if not args.inflight_path:
                request_armed.set()
                return
            requests = await run_long_requests(
                bus=EventBus(),
                host=target_host,
                port=args.target_port,
                method="GET",
                path=args.inflight_path,
                headers={},
                concurrent=args.concurrent,
                timeout_ms=args.inflight_timeout_ms,
                request_sent_event=request_armed,
            )

        async def signal_target() -> None:
            nonlocal sigterm_ns
            await accept_armed.wait()
            await request_armed.wait()
            if args.sigterm_after_ms:
                await anyio.sleep(args.sigterm_after_ms / 1000)
            sigterm_ns = time.monotonic_ns()
            await anyio.to_thread.run_sync(docker.signal, target_id, "SIGTERM")
            signal_sent.set()

        async def wait_target() -> None:
            nonlocal exit_code
            exit_code = await anyio.to_thread.run_sync(docker.wait, target_id)
            exited.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(accept_watcher)
            task_group.start_soon(inflight_traffic)
            task_group.start_soon(signal_target)
            task_group.start_soon(wait_target)

        accepted = [
            attempt.connected_ns
            for attempt in accept_attempts
            if attempt.connected_ns is not None
        ]
        accept_window_ms = (
            None if not accepted else (max(accepted) - sigterm_ns) / 1_000_000
        )
        after_t0 = [
            attempt for attempt in accept_attempts if attempt.started_ns >= sigterm_ns
        ]
        return {
            "schema": "sidecar-probe-spike-v1",
            "environment": args.environment,
            "probe_location": args.probe_location,
            "fixture": args.fixture,
            "probe_startup_ms": (
                None
                if args.launch_requested_unix_ns == 0
                else (process_started_ns - args.launch_requested_unix_ns) / 1_000_000
            ),
            "target_start_overhead_ms": (
                target_started_ns - target_start_requested_ns
            )
            / 1_000_000,
            "tcp_open_ms": tcp_open_ms,
            "readiness_ms": readiness_ms,
            "readiness_p50_ms": statistics.median(readiness_latencies),
            "measurement_jitter_ms": statistics.median(jitter_samples),
            "measurement_jitter_samples_ms": jitter_samples,
            "teardown_floor_ms": statistics.median(floor_samples),
            "teardown_floor_samples_ms": floor_samples,
            "accept_window_ms": accept_window_ms,
            "accept_probe_interval_ms": ACCEPT_PROBE_INTERVAL_MS,
            "accept_attempts_after_t0": [
                _attempt_dict(attempt, sigterm_ns) for attempt in after_t0
            ],
            "accept_then_reset": sum(
                1
                for attempt in after_t0
                if attempt.connected_ns is not None
                and attempt.outcome is AcceptOutcome.RESET
            ),
            "inflight": _inflight_evidence(requests, sigterm_ns),
            "exit_code": exit_code,
        }
    finally:
        await anyio.to_thread.run_sync(docker.remove, target_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--probe-location", choices=("host", "sidecar"), required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--target-port", type=int, default=8000)
    parser.add_argument("--target-env-json", default="{}")
    parser.add_argument("--target-command-json")
    parser.add_argument("--ready-path", default="/ready")
    parser.add_argument("--inflight-path")
    parser.add_argument("--concurrent", type=int, default=10)
    parser.add_argument("--sigterm-after-ms", type=int, default=0)
    parser.add_argument("--inflight-timeout-ms", type=int, default=40_000)
    parser.add_argument("--jitter-samples", type=int, default=15)
    parser.add_argument("--floor-samples", type=int, default=5)
    parser.add_argument("--docker-socket", default="/var/run/docker.sock")
    parser.add_argument("--launch-requested-unix-ns", type=int, default=0)
    return parser


def main() -> None:
    try:
        result = anyio.run(_measure, _parser().parse_args())
    except Exception as error:  # pragma: no cover - spike boundary diagnostics
        print(f"sidecar probe failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
