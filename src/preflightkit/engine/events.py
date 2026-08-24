"""Lifecycle events.

Every observation the tool makes lands here first. Contracts read this stream and
never touch Docker themselves — that separation is what lets a Kubernetes or
process runtime slot in later without rewriting a single contract.

All timestamps come from `time.monotonic_ns()`. The nanosecond resolution is the
source's, not the measurement's: anything observed through the Docker daemon
carries that daemon's round-trip latency. See `RunReport.measurement_jitter_ms`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def now_ns() -> int:
    return time.monotonic_ns()


class RequestPhase(StrEnum):
    """Where a request was when it died. The distinction is the whole point."""

    CONNECTING = "connecting"
    SENDING = "sending"
    AWAITING_RESPONSE = "awaiting_response"
    READING_BODY = "reading_body"


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    timestamp_ns: int = field(default_factory=now_ns)
    data: dict[str, Any] = field(default_factory=dict)

    def offset_ms_from(self, origin_ns: int) -> float:
        return (self.timestamp_ns - origin_ns) / 1_000_000


class Kind(StrEnum):
    CONTAINER_STARTED = "ContainerStarted"
    PORT_OPENED = "PortOpened"
    READINESS_PASSED = "ReadinessPassed"
    BASELINE_MEASURED = "BaselineMeasured"
    READINESS_FAILED = "ReadinessFailed"
    REQUEST_STARTED = "RequestStarted"
    REQUEST_COMPLETED = "RequestCompleted"
    REQUEST_RESET = "RequestReset"
    CONNECTION_REFUSED = "ConnectionRefused"
    SIGNAL_SENT = "SignalSent"
    PROCESS_EXITED = "ProcessExited"
    TIMEOUT_REACHED = "TimeoutReached"


def event(kind: Kind, timestamp_ns: int | None = None, **data: Any) -> Event:
    return Event(
        kind=str(kind),
        timestamp_ns=now_ns() if timestamp_ns is None else timestamp_ns,
        data=data,
    )
