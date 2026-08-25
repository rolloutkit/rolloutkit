"""Readiness and TCP probing.

httpx is fine here: a readiness poll needs a status code and a latency, none of
the connection-teardown detail the traffic client cares about.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio
import httpx

from preflightkit.engine.events import now_ns

READINESS_POLL_INTERVAL_MS = 100


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    status: int | None
    latency_ns: int
    headers: dict[str, str] | None = None
    body_head: str = ""
    body_head_bytes: int = 0
    error: str | None = None


async def tcp_open(host: str, port: int, timeout_ms: int = 1000) -> bool:
    """Whether a TCP connection can be established.

    Note what this does *not* prove: thanks to the listen backlog the kernel
    completes the handshake whether or not the application ever calls accept().
    Port-open is a startup signal, never a proof that requests are being served.
    """
    try:
        with anyio.fail_after(timeout_ms / 1000):
            stream = await anyio.connect_tcp(host, port)
            await stream.aclose()
        return True
    except (OSError, TimeoutError):
        return False


async def probe_http(
    client: httpx.AsyncClient,
    url: str,
    *,
    expected_status: int,
    timeout_ms: int = 2000,
) -> ProbeResult:
    start = now_ns()
    try:
        response = await client.get(url, timeout=timeout_ms / 1000)
    except httpx.HTTPError as exc:
        return ProbeResult(False, None, now_ns() - start, error=f"{type(exc).__name__}: {exc}")
    return ProbeResult(
        ok=response.status_code == expected_status,
        status=response.status_code,
        latency_ns=now_ns() - start,
        headers=dict(response.headers),
        body_head=response.content[:256].decode("utf-8", errors="replace"),
        body_head_bytes=min(len(response.content), 256),
    )


async def wait_for_tcp(
    host: str, port: int, *, budget_ms: int, interval_ms: int = 50
) -> int | None:
    """Poll until the port accepts connections. Returns the timestamp, or None."""
    deadline = now_ns() + budget_ms * 1_000_000
    while now_ns() < deadline:
        if await tcp_open(host, port, timeout_ms=min(1000, interval_ms * 10)):
            return now_ns()
        await anyio.sleep(interval_ms / 1000)
    return None


async def wait_for_readiness(
    client: httpx.AsyncClient,
    url: str,
    *,
    expected_status: int,
    budget_ms: int,
    interval_ms: int = READINESS_POLL_INTERVAL_MS,
) -> tuple[int | None, ProbeResult | None]:
    """Poll readiness until it passes. Returns (timestamp, last result)."""
    deadline = now_ns() + budget_ms * 1_000_000
    last: ProbeResult | None = None
    while now_ns() < deadline:
        last = await probe_http(client, url, expected_status=expected_status)
        if last.ok:
            return now_ns(), last
        await anyio.sleep(interval_ms / 1000)
    return None, last
