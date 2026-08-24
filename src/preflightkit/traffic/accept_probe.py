"""Low-rate new-connection stream for SP004's drain window."""

from __future__ import annotations

import errno
from dataclasses import dataclass
from enum import StrEnum

import anyio
from anyio import BrokenResourceError, EndOfStream

from preflightkit.engine.events import now_ns

ACCEPT_PROBE_INTERVAL_MS = 50
ACCEPT_PROBE_TIMEOUT_MS = 500
TERMINAL_REFUSED_STREAK = 3


class AcceptOutcome(StrEnum):
    RESPONSE = "response"
    CLOSED_WITHOUT_RESPONSE = "closed_without_response"
    REFUSED = "connection_refused"
    TIMEOUT = "timeout"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class AcceptAttempt:
    started_ns: int
    connected_ns: int | None
    finished_ns: int
    outcome: AcceptOutcome
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.connected_ns is not None


async def probe_new_connection(
    *, host: str, port: int, path: str, timeout_ms: int = ACCEPT_PROBE_TIMEOUT_MS
) -> AcceptAttempt:
    """Open one socket, send one request, and classify how the peer ends it."""
    started_ns = now_ns()
    connected_ns: int | None = None
    stream: anyio.abc.SocketStream | None = None
    try:
        with anyio.fail_after(timeout_ms / 1000):
            try:
                stream = await anyio.connect_tcp(host, port)
            except OSError as exc:
                code = _root_errno(exc)
                outcome = (
                    AcceptOutcome.TIMEOUT
                    if code == errno.ETIMEDOUT
                    else AcceptOutcome.REFUSED
                )
                return AcceptAttempt(
                    started_ns, None, now_ns(), outcome, _describe(exc, code)
                )

            connected_ns = now_ns()
            async with stream:
                request = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                await stream.send(request)
                try:
                    data = await stream.receive()
                except EndOfStream:
                    return AcceptAttempt(
                        started_ns,
                        connected_ns,
                        now_ns(),
                        AcceptOutcome.CLOSED_WITHOUT_RESPONSE,
                    )
                return AcceptAttempt(
                    started_ns,
                    connected_ns,
                    now_ns(),
                    AcceptOutcome.RESPONSE
                    if data
                    else AcceptOutcome.CLOSED_WITHOUT_RESPONSE,
                )
    except TimeoutError:
        return AcceptAttempt(
            started_ns,
            connected_ns,
            now_ns(),
            AcceptOutcome.TIMEOUT,
            f"no response within {timeout_ms}ms",
        )
    except (OSError, BrokenResourceError) as exc:
        return AcceptAttempt(
            started_ns,
            connected_ns,
            now_ns(),
            AcceptOutcome.RESET,
            _describe(exc, _root_errno(exc)),
        )


def _root_errno(exc: BaseException) -> int | None:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno is not None:
            return current.errno
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.extend(item for item in (current.__cause__, current.__context__) if item)
    return None


def _describe(exc: BaseException, code: int | None) -> str:
    name = errno.errorcode.get(code, "") if code is not None else ""
    text = getattr(exc, "strerror", None) or str(exc)
    return f"{name}: {text}" if name else text
