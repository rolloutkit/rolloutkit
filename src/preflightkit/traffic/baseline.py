"""A short burst of ordinary traffic, taken before the experiment begins.

Two problems this solves, both found on a real image.

The first is that a completed request is not the same as a working one. A
service whose database has no schema answers every call with a 500, and every
one of those 500s arrives intact, on time, with a clean connection. SP005 would
report 200/200 completed and mean nothing by it. So before measuring anything,
ask the service whether it works, and refuse to publish a verdict about a
service that does not.

The second is that `sigterm_after` was a number the user had to guess. It has to
land *inside* a request, and the only way to know where that is is to find out
how long a request takes. That is measurable, so measure it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import anyio
import httpx

from preflightkit.probes.http import ProbeResult, probe_http
from preflightkit.traffic.client import (
    Outcome,
    RequestResult,
    perform_request,
    verify_keep_alive,
)

#: Enough samples for a median that does not swing on one slow call, few enough
#: that the burst itself stays a rounding error next to container startup.
BASELINE_SAMPLES = 25

#: Sent concurrently rather than one after another: a service with a genuinely
#: slow endpoint — the case SP005 was designed for — would otherwise spend
#: `25 x duration` here, which for the 5s fixture is over two minutes.
BASELINE_CONCURRENCY = 25

READINESS_STABILITY_SAMPLES = 10
READINESS_BODY_HEAD_BYTES = 256


@dataclass(slots=True)
class ReadinessBaseline:
    """Sequential readiness observations from the existing steady-state phase."""

    samples: list[ProbeResult] = field(default_factory=list)
    health_sample: ProbeResult | None = None

    @property
    def latencies_ms(self) -> list[float]:
        return [sample.latency_ns / 1_000_000 for sample in self.samples]

    @property
    def p50_ms(self) -> float | None:
        return statistics.median(self.latencies_ms) if self.samples else None

    @property
    def max_ms(self) -> float | None:
        return max(self.latencies_ms) if self.samples else None

    def as_dict(self) -> dict[str, object]:
        return {
            "n": len(self.samples),
            "p50_ms": _round(self.p50_ms),
            "max_ms": _round(self.max_ms),
            "body_head_limit_bytes": READINESS_BODY_HEAD_BYTES,
            "samples": [_probe_dict(sample) for sample in self.samples],
            "health_sample": (
                _probe_dict(self.health_sample)
                if self.health_sample is not None
                else None
            ),
        }


@dataclass(slots=True)
class Baseline:
    """What the service did when nobody was shutting it down."""

    samples: int = 0
    completed: int = 0
    succeeded: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    durations_ms: list[float] = field(default_factory=list)
    keep_alive_established: bool | None = None

    @property
    def healthy(self) -> bool:
        """Every sample completed and every status was 2xx.

        Strict on purpose. This is a small burst against a service that just
        reported itself ready, with no other load on it and no signal sent. One
        non-2xx here is not noise — it is the service telling us that what we are
        about to measure is not a working request path.
        """
        return self.samples > 0 and self.succeeded == self.samples

    @property
    def p50_ms(self) -> float | None:
        return statistics.median(self.durations_ms) if self.durations_ms else None

    @property
    def p90_ms(self) -> float | None:
        """Nearest-rank, no interpolation.

        The value reported is one that was actually observed. An interpolated
        percentile invents a duration no request had, which is the wrong kind of
        number to print next to measurements.
        """
        if not self.durations_ms:
            return None
        ordered = sorted(self.durations_ms)
        index = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
        return ordered[index]

    @property
    def min_ms(self) -> float | None:
        return min(self.durations_ms) if self.durations_ms else None

    @property
    def max_ms(self) -> float | None:
        return max(self.durations_ms) if self.durations_ms else None

    @property
    def suggested_sigterm_after_ms(self) -> int | None:
        """Where to aim the signal so it lands mid-request.

        Half the median. Early enough that the slower half of the distribution is
        still running when the signal arrives, late enough that the request is
        genuinely established rather than still connecting. Rounded up to at
        least 1ms because a 0ms wait would race the request's own start.
        """
        p50 = self.p50_ms
        return None if p50 is None else max(1, round(p50 / 2))

    def as_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "completed": self.completed,
            "succeeded": self.succeeded,
            "healthy": self.healthy,
            "statuses": dict(sorted(self.statuses.items())),
            "outcomes": dict(sorted(self.outcomes.items())),
            "p50_ms": _round(self.p50_ms),
            "p90_ms": _round(self.p90_ms),
            "min_ms": _round(self.min_ms),
            "max_ms": _round(self.max_ms),
            "suggested_sigterm_after_ms": self.suggested_sigterm_after_ms,
            "keep_alive_established": self.keep_alive_established,
            "keep_alive_applicable": self.keep_alive_established is True,
        }

    def describe(self) -> str:
        """One-line summary, used in the message that stops the run."""
        parts = [f"{self.succeeded}/{self.samples} succeeded"]
        if self.statuses:
            seen = ", ".join(f"{k} x{v}" for k, v in sorted(self.statuses.items()))
            parts.append(f"statuses: {seen}")
        broken = {k: v for k, v in self.outcomes.items() if k != Outcome.COMPLETED}
        if broken:
            seen = ", ".join(f"{k} x{v}" for k, v in sorted(broken.items()))
            parts.append(f"outcomes: {seen}")
        if self.p50_ms is not None:
            parts.append(f"p50 {self.p50_ms:.1f}ms")
        return "; ".join(parts)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def summarise(results: list[RequestResult]) -> Baseline:
    baseline = Baseline(samples=len(results))
    for result in results:
        key = str(result.outcome)
        baseline.outcomes[key] = baseline.outcomes.get(key, 0) + 1
        if result.status is not None:
            label = str(result.status)
            baseline.statuses[label] = baseline.statuses.get(label, 0) + 1
        if result.outcome is not Outcome.COMPLETED:
            continue
        baseline.completed += 1
        if result.status is not None and 200 <= result.status < 300:
            baseline.succeeded += 1
        if result.finished_ns is not None:
            # `started_ns` is stamped by perform_request before anything else, so
            # a zero here means a synthetic result, not a missing measurement —
            # guarding on its truthiness would silently drop it.
            baseline.durations_ms.append(
                (result.finished_ns - result.started_ns) / 1_000_000
            )
    return baseline


async def measure_baseline(
    *,
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    timeout_ms: int,
    samples: int = BASELINE_SAMPLES,
    concurrency: int = BASELINE_CONCURRENCY,
) -> Baseline:
    """Send `samples` requests at a ready, unsignalled service and describe them.

    Deliberately not routed through the event bus. These requests are not part of
    the experiment — putting them on the timeline would bury the handful of
    events that describe the actual shutdown under 25 that describe nothing.
    """
    results: list[RequestResult | None] = [None] * samples
    limiter = anyio.CapacityLimiter(max(1, concurrency))

    async def one(index: int) -> None:
        async with limiter:
            results[index] = await perform_request(
                request_id=-(index + 1),
                host=host,
                port=port,
                method=method,
                path=path,
                headers=headers,
                timeout_ms=timeout_ms,
            )

    async with anyio.create_task_group() as tg:
        for index in range(samples):
            tg.start_soon(one, index)

    baseline = summarise([r for r in results if r is not None])
    baseline.keep_alive_established = await verify_keep_alive(
        host=host,
        port=port,
        method=method,
        path=path,
        headers=headers,
        timeout_ms=timeout_ms,
    )
    return baseline


async def measure_readiness_baseline(
    *,
    host: str,
    port: int,
    readiness_path: str,
    readiness_status: int,
    health_path: str | None = None,
    health_status: int | None = None,
    timeout_ms: int = 5000,
) -> ReadinessBaseline:
    """Measure readiness sequentially inside the existing baseline phase."""
    baseline = ReadinessBaseline()
    async with httpx.AsyncClient(base_url=f"http://{host}:{port}") as client:
        for _ in range(READINESS_STABILITY_SAMPLES):
            baseline.samples.append(
                await probe_http(
                    client,
                    readiness_path,
                    expected_status=readiness_status,
                    timeout_ms=timeout_ms,
                )
            )
        if health_path is not None and health_status is not None:
            baseline.health_sample = await probe_http(
                client,
                health_path,
                expected_status=health_status,
                timeout_ms=timeout_ms,
            )
    return baseline


def _probe_dict(sample: ProbeResult) -> dict[str, object]:
    return {
        "status": sample.status,
        "ok": sample.ok,
        "latency_ms": _round(sample.latency_ns / 1_000_000),
        "headers": sample.headers or {},
        "body_head": sample.body_head,
        "body_head_bytes": sample.body_head_bytes,
        "error": sample.error,
    }
