"""The traffic client's outcome classification.

These are the distinctions the whole product rests on: a refused connection is
normal, a destroyed in-flight response is a defect, and a clean close after a
complete response is neither.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Awaitable, Callable

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream

from preflightkit.traffic.client import (
    Outcome,
    RequestResult,
    perform_request,
    verify_keep_alive,
)

pytestmark = pytest.mark.anyio

Handler = Callable[[SocketStream], Awaitable[None]]


async def _serve(handler: Handler, task_status) -> None:
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_address)[1]
    task_status.started(port)
    await listener.serve(handler)


async def _request(handler: Handler, timeout_ms: int = 3000):
    async with anyio.create_task_group() as tg:
        port = await tg.start(_serve, handler)
        result = await perform_request(
            request_id=1,
            host="127.0.0.1",
            port=port,
            method="GET",
            path="/slow",
            headers={},
            timeout_ms=timeout_ms,
        )
        tg.cancel_scope.cancel()
    return result


async def _verify_keep_alive(handler: Handler) -> bool:
    async with anyio.create_task_group() as tg:
        port = await tg.start(_serve, handler)
        established = await verify_keep_alive(
            host="127.0.0.1",
            port=port,
            method="GET",
            path="/work",
            headers={},
            timeout_ms=3000,
        )
        tg.cancel_scope.cancel()
    return established


async def _hard_reset(stream: SocketStream) -> None:
    """Close with SO_LINGER 0 so the kernel sends RST rather than FIN.

    The raw socket asyncio hands back is a ``TransportSocket``: it forwards
    setsockopt but refuses close, because the transport owns the descriptor. So
    the option is set on the raw socket and the close goes through the stream.
    """
    raw = stream.extra(SocketAttribute.raw_socket)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    await stream.aclose()


async def test_complete_response_with_content_length() -> None:
    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await stream.send(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        )
        await stream.aclose()

    result = await _request(handler)
    assert result.outcome is Outcome.COMPLETED
    assert result.status == 200
    assert result.body_bytes == 5


async def test_complete_chunked_response() -> None:
    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await stream.send(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n"
        )
        await stream.aclose()

    result = await _request(handler)
    assert result.outcome is Outcome.COMPLETED
    assert result.body_bytes == 10


async def test_reset_mid_response_is_a_defect() -> None:
    """Headers promised 100 bytes, 10 arrived, then the connection died."""

    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await stream.send(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n0123456789")
        # An RST discards whatever is still sitting in the client's receive
        # buffer, so give the client time to read the headers first — otherwise
        # the test would be measuring the race, not the classification.
        await anyio.sleep(0.1)
        await _hard_reset(stream)

    result = await _request(handler)
    assert result.outcome is Outcome.RESET_MID_RESPONSE
    assert result.body_bytes == 0  # nothing complete was delivered
    assert result.expected_body_bytes == 100
    assert result.first_byte_ns is not None


async def test_reset_before_any_response() -> None:
    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await _hard_reset(stream)

    result = await _request(handler)
    assert result.outcome is Outcome.RESET_BEFORE_RESPONSE
    assert result.first_byte_ns is None


async def test_clean_close_before_response_is_still_a_defect() -> None:
    """os._exit() closes sockets with FIN, not RST. Same damage to the client."""

    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await stream.aclose()

    result = await _request(handler)
    assert result.outcome is Outcome.RESET_BEFORE_RESPONSE
    assert result.error_detail is not None and "FIN" in result.error_detail


async def test_refused_connection_is_not_a_defect() -> None:
    """After the listener closes this is the expected, correct outcome."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_address)[1]
    await listener.aclose()

    result = await perform_request(
        request_id=1,
        host="127.0.0.1",
        port=port,
        method="GET",
        path="/",
        headers={},
        timeout_ms=2000,
    )
    assert result.outcome is Outcome.REFUSED
    assert result.connected_ns is None


async def test_timeout_when_server_never_answers() -> None:
    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await anyio.sleep(30)

    result = await _request(handler, timeout_ms=300)
    assert result.outcome is Outcome.TIMEOUT


async def test_request_sent_event_fires_before_the_response_completes() -> None:
    request_received = anyio.Event()
    release_response = anyio.Event()

    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        request_received.set()
        await release_response.wait()
        await stream.send(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")

    async with anyio.create_task_group() as tg:
        port = await tg.start(_serve, handler)
        request_sent = anyio.Event()
        result: list[RequestResult] = []

        async def run() -> None:
            result.append(
                await perform_request(
                    request_id=1,
                    host="127.0.0.1",
                    port=port,
                    method="GET",
                    path="/slow",
                    headers={},
                    timeout_ms=3000,
                    request_sent_event=request_sent,
                )
            )

        tg.start_soon(run)
        await request_sent.wait()
        await request_received.wait()
        assert result == []
        release_response.set()
        while not result:
            await anyio.sleep(0)
        tg.cancel_scope.cancel()

    assert result[0].outcome is Outcome.COMPLETED


async def test_connection_close_header_is_recorded() -> None:
    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await stream.send(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi"
        )
        await stream.aclose()

    result = await _request(handler)
    assert result.outcome is Outcome.COMPLETED
    assert result.connection_close is True


async def test_keep_alive_requires_two_requests_on_one_connection() -> None:
    async def handler(stream: SocketStream) -> None:
        for _ in range(2):
            await stream.receive()
            await stream.send(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")

    assert await _verify_keep_alive(handler) is True


async def test_connection_close_does_not_establish_keep_alive() -> None:
    async def handler(stream: SocketStream) -> None:
        await stream.receive()
        await stream.send(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi"
        )
        await stream.aclose()

    assert await _verify_keep_alive(handler) is False
