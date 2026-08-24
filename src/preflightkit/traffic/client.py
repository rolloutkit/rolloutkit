"""A minimal HTTP/1.1 client built directly on anyio streams.

httpx is not used here on purpose. It collapses distinctions this tool exists to
report:

  * ECONNREFUSED (a normal, expected outcome once the listener is gone) and
    ECONNRESET (a defect: an accepted request was torn down) surface as the same
    class of error.
  * When a response dies half-way through, the count of bytes actually delivered
    is lost — and that count is the difference between "the server closed
    cleanly" and "the server dropped an in-flight response".
  * Whether the server sent `Connection: close` before going away is invisible.

httpx remains the right tool for readiness polling, where none of this matters.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass, field
from enum import StrEnum

import anyio
from anyio import BrokenResourceError, EndOfStream

from preflightkit.engine.events import RequestPhase, now_ns


class Outcome(StrEnum):
    COMPLETED = "completed"
    REFUSED = "refused"
    RESET_BEFORE_RESPONSE = "reset_before_response"
    RESET_MID_RESPONSE = "reset_mid_response"
    TIMEOUT = "timeout"


#: Outcomes that mean an already-accepted request was destroyed. These are the
#: only ones SP005 treats as defects — a refused *new* connection is expected.
BROKEN_OUTCOMES = frozenset(
    {Outcome.RESET_BEFORE_RESPONSE, Outcome.RESET_MID_RESPONSE, Outcome.TIMEOUT}
)


@dataclass(slots=True)
class RequestResult:
    request_id: int
    outcome: Outcome = Outcome.TIMEOUT
    phase: RequestPhase = RequestPhase.CONNECTING
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body_bytes: int = 0
    expected_body_bytes: int | None = None
    started_ns: int = 0
    connected_ns: int | None = None
    request_sent_ns: int | None = None
    first_byte_ns: int | None = None
    finished_ns: int | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.COMPLETED

    @property
    def connection_close(self) -> bool:
        return self.headers.get("connection", "").lower() == "close"

    @property
    def keep_alive_established(self) -> bool:
        """Whether this response left an HTTP/1.1 connection reusable.

        A response with `Connection: close`, or one framed only by EOF, cannot
        establish keep-alive. This deliberately distinguishes sync gunicorn,
        which closes every response, from servers where a later shutdown-time
        `Connection: close` announcement carries information.
        """
        if self.outcome is not Outcome.COMPLETED or self.connection_close:
            return False
        no_body = self.status in (204, 304) or (
            self.status is not None and 100 <= self.status < 200
        )
        return (
            "content-length" in self.headers
            or "chunked" in self.headers.get("transfer-encoding", "").lower()
            or no_body
        )


class _Reader:
    """Buffered reader that tracks when the first byte actually arrived."""

    def __init__(self, stream: anyio.abc.SocketStream, result: RequestResult) -> None:
        self._stream = stream
        self._result = result
        self._buf = bytearray()

    async def _fill(self) -> None:
        chunk = await self._stream.receive()
        if self._result.first_byte_ns is None:
            self._result.first_byte_ns = now_ns()
        self._buf.extend(chunk)

    async def read_until(self, sep: bytes, limit: int = 64 * 1024) -> bytes:
        while True:
            index = self._buf.find(sep)
            if index != -1:
                out = bytes(self._buf[:index])
                del self._buf[: index + len(sep)]
                return out
            if len(self._buf) > limit:
                raise ValueError(f"no {sep!r} within {limit} bytes")
            await self._fill()

    async def read_exactly(self, count: int) -> bytes:
        while len(self._buf) < count:
            await self._fill()
        out = bytes(self._buf[:count])
        del self._buf[:count]
        return out

    async def drain_until_close(self) -> int:
        """HTTP/1.0-style body: read until the peer closes."""
        total = len(self._buf)
        self._buf.clear()
        while True:
            try:
                await self._fill()
            except EndOfStream:
                return total
            total += len(self._buf)
            self._buf.clear()


#: The connection was torn down rather than closed: the peer sent RST, or wrote
#: to a socket the peer had already destroyed.
_RESET_ERRNOS = frozenset({errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED})


def _root_errno(exc: BaseException) -> int | None:
    """Dig the real errno out of a connect failure.

    ``anyio.connect_tcp`` runs happy-eyeballs across every resolved address and,
    when they all fail, raises a bare ``OSError("All connection attempts
    failed")`` whose own ``errno`` is ``None``. The actual code lives in
    ``__cause__`` — a single OSError for one address, an exception group for
    several. Reading ``exc.errno`` directly here silently classifies every
    refused connection as something else, which turns the normal
    "listener is gone" case into a reported defect.
    """
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
        stack.extend(e for e in (current.__cause__, current.__context__) if e)
    return None


def _describe_os_error(exc: BaseException, code: int | None) -> str:
    name = errno.errorcode.get(code, "") if code is not None else ""
    text = getattr(exc, "strerror", None) or str(exc)
    return f"{name}: {text}" if name else text


async def perform_request(
    *,
    request_id: int,
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    timeout_ms: int,
) -> RequestResult:
    """Run one request on its own connection and classify how it ended."""
    result = RequestResult(request_id=request_id, started_ns=now_ns())
    try:
        with anyio.fail_after(timeout_ms / 1000):
            await _run(result, host, port, method, path, headers)
    except TimeoutError:
        # Only if the deadline actually beat the response — a request that landed
        # just as the scope expired stays completed.
        if result.outcome is not Outcome.COMPLETED:
            result.outcome = Outcome.TIMEOUT
            result.error_detail = f"no completion within {timeout_ms}ms"
    if result.finished_ns is None:
        result.finished_ns = now_ns()
    return result


async def _run(
    result: RequestResult,
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    try:
        stream = await anyio.connect_tcp(host, port)
    except OSError as exc:
        # The connection never came up, so no request was ever in flight and
        # nothing could have been destroyed. Whatever the errno says, this is
        # never an SP005 defect — after the listener closes it is exactly what a
        # load balancer is supposed to see. The one distinction worth keeping is
        # ETIMEDOUT: the packets vanished instead of being answered, which points
        # at the network rather than at the application.
        code = _root_errno(exc)
        result.outcome = Outcome.TIMEOUT if code == errno.ETIMEDOUT else Outcome.REFUSED
        result.error_detail = f"connect: {_describe_os_error(exc, code)}"
        result.finished_ns = now_ns()
        return

    result.connected_ns = now_ns()
    async with stream:
        await _exchange(stream, result, host, port, method, path, headers)


async def verify_keep_alive(
    *,
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    timeout_ms: int,
) -> bool:
    """Prove that one connection accepts two complete sequential requests."""
    try:
        with anyio.fail_after(timeout_ms / 1000):
            stream = await anyio.connect_tcp(host, port)
            async with stream:
                for request_id in (-10_001, -10_002):
                    result = RequestResult(
                        request_id=request_id,
                        started_ns=now_ns(),
                        connected_ns=now_ns(),
                    )
                    await _exchange(stream, result, host, port, method, path, headers)
                    if not result.keep_alive_established:
                        return False
                return True
    except (TimeoutError, OSError, BrokenResourceError):
        return False


async def _exchange(
    stream: anyio.abc.SocketStream,
    result: RequestResult,
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    # No Connection header is sent: we want to observe the server's default
    # keep-alive behavior, not force its hand.
    lines = [f"{method.upper()} {path} HTTP/1.1", f"Host: {host}:{port}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    wire = ("\r\n".join(lines) + "\r\n\r\n").encode()

    result.phase = RequestPhase.SENDING
    try:
        await stream.send(wire)
    except (OSError, BrokenResourceError) as exc:
        result.outcome = Outcome.RESET_BEFORE_RESPONSE
        result.error_detail = f"send: {_describe_os_error(exc, _root_errno(exc))}"
        result.finished_ns = now_ns()
        return
    result.request_sent_ns = now_ns()
    result.phase = RequestPhase.AWAITING_RESPONSE

    reader = _Reader(stream, result)
    try:
        await _read_response(reader, result)
    except EndOfStream:
        _classify_break(result, "peer closed the connection (FIN)")
    except (OSError, BrokenResourceError) as exc:
        # anyio does not let the OSError through: a reset surfaces as
        # BrokenResourceError with the real error hidden in __cause__.
        code = _root_errno(exc)
        detail = _describe_os_error(exc, code)
        _classify_break(
            result,
            f"connection reset (RST): {detail}" if code in _RESET_ERRNOS else detail,
        )
    except ValueError as exc:
        _classify_break(result, f"malformed response: {exc}")


def _classify_break(result: RequestResult, detail: str) -> None:
    """A connection died. Whether that is a defect depends on where it died."""
    if result.first_byte_ns is None:
        result.outcome = Outcome.RESET_BEFORE_RESPONSE
    else:
        result.outcome = Outcome.RESET_MID_RESPONSE
    result.error_detail = detail
    result.finished_ns = now_ns()


async def _read_response(reader: _Reader, result: RequestResult) -> None:
    status_line = (await reader.read_until(b"\r\n")).decode("latin-1")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].upper().startswith("HTTP/"):
        raise ValueError(f"bad status line {status_line!r}")
    result.status = int(parts[1])

    raw_headers = (await reader.read_until(b"\r\n\r\n")).decode("latin-1")
    for line in raw_headers.split("\r\n"):
        if not line:
            continue
        name, _, value = line.partition(":")
        result.headers[name.strip().lower()] = value.strip()

    result.phase = RequestPhase.READING_BODY
    encoding = result.headers.get("transfer-encoding", "").lower()
    length = result.headers.get("content-length")

    if "chunked" in encoding:
        result.body_bytes = await _read_chunked(reader)
    elif length is not None:
        expected = int(length)
        result.expected_body_bytes = expected
        result.body_bytes = len(await reader.read_exactly(expected))
    elif result.status in (204, 304) or 100 <= result.status < 200:
        result.body_bytes = 0
    else:
        # No framing: the body ends when the connection does. A clean EOF here is
        # a complete response, not a truncation.
        result.body_bytes = await reader.drain_until_close()

    result.outcome = Outcome.COMPLETED
    result.finished_ns = now_ns()


async def _read_chunked(reader: _Reader) -> int:
    total = 0
    while True:
        line = (await reader.read_until(b"\r\n")).decode("latin-1").strip()
        size = int(line.split(";", 1)[0], 16)
        if size == 0:
            await reader.read_until(b"\r\n")  # trailers terminator
            return total
        total += len(await reader.read_exactly(size))
        await reader.read_exactly(2)  # trailing CRLF
