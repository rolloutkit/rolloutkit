"""Traffic generation for the in-flight experiment."""

from __future__ import annotations

import anyio

from rolloutkit.engine.bus import EventBus
from rolloutkit.engine.events import Kind, event
from rolloutkit.traffic.client import Outcome, RequestResult, perform_request


def _emit(bus: EventBus, result: RequestResult) -> None:
    if result.outcome is Outcome.COMPLETED:
        bus.record(
            event(
                Kind.REQUEST_COMPLETED,
                timestamp_ns=result.finished_ns,
                request_id=result.request_id,
                status=result.status,
                body_bytes=result.body_bytes,
                connection_close=result.connection_close,
            )
        )
    elif result.outcome is Outcome.REFUSED:
        bus.record(
            event(
                Kind.CONNECTION_REFUSED,
                timestamp_ns=result.finished_ns,
                request_id=result.request_id,
            )
        )
    else:
        bus.record(
            event(
                Kind.REQUEST_RESET,
                timestamp_ns=result.finished_ns,
                request_id=result.request_id,
                phase=str(result.phase),
                outcome=str(result.outcome),
                status=result.status,
                body_bytes=result.body_bytes,
                expected_body_bytes=result.expected_body_bytes,
                detail=result.error_detail,
            )
        )


async def run_long_requests(
    *,
    bus: EventBus,
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    concurrent: int,
    timeout_ms: int,
    request_sent_event: anyio.Event | None = None,
) -> list[RequestResult]:
    """Fire N concurrent slow requests and wait for every one of them to settle.

    Nothing here knows about SIGTERM. The signal is sent by the lifecycle while
    these are in flight — which is precisely the condition SP005 measures.
    """
    results: list[RequestResult | None] = [None] * concurrent

    async def one(index: int) -> None:
        request_id = index + 1
        bus.record(event(Kind.REQUEST_STARTED, request_id=request_id))
        result = await perform_request(
            request_id=request_id,
            host=host,
            port=port,
            method=method,
            path=path,
            headers=headers,
            timeout_ms=timeout_ms,
            request_sent_event=request_sent_event,
        )
        results[index] = result
        _emit(bus, result)

    async with anyio.create_task_group() as tg:
        for index in range(concurrent):
            tg.start_soon(one, index)

    return [r for r in results if r is not None]
