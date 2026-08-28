"""SP002 readiness stability verdicts and their retained evidence."""

from __future__ import annotations

import pytest

from rolloutkit.config.models import Config, Contracts, ReadinessContract, Target
from rolloutkit.contracts.base import Status
from rolloutkit.contracts.readiness import ReadinessStabilityContract
from rolloutkit.engine.context import RunReport
from rolloutkit.probes.http import ProbeResult
from rolloutkit.traffic.baseline import ReadinessBaseline


def _sample(
    status: int = 200,
    *,
    latency_ms: float = 10,
    body: str = '{"status": "ready"}',
    headers: dict[str, str] | None = None,
) -> ProbeResult:
    return ProbeResult(
        ok=status == 200,
        status=status,
        latency_ns=int(latency_ms * 1_000_000),
        headers=headers or {"content-type": "application/json"},
        body_head=body,
        body_head_bytes=len(body.encode()),
    )


def _report(
    samples: list[ProbeResult] | None,
    *,
    health: ProbeResult | None = None,
    latency_budget: str = "500ms",
) -> RunReport:
    config = Config(
        target=Target(image="fixture:test", port=8000),
        contracts=Contracts(
            readiness=ReadinessContract(latency_budget=latency_budget)
        ),
    )
    report = RunReport(config=config)
    if samples is not None:
        report.readiness_baseline = ReadinessBaseline(samples, health)
    return report


def test_sp002_flapping_fails_and_retains_every_probe() -> None:
    samples = [_sample(200 if index % 2 == 0 else 503) for index in range(10)]

    result = ReadinessStabilityContract().evaluate(_report(samples))

    assert (result.status, result.branch) == (Status.FAIL, "flapping")
    assert [item["status"] for item in result.evidence["samples"]] == [
        200,
        503,
    ] * 5
    assert result.evidence["n"] == 10
    assert all(
        {"status", "latency_ms", "headers", "body_head", "body_head_bytes"}
        <= item.keys()
        for item in result.evidence["samples"]
    )


def test_sp002_stably_incorrect_readiness_fails() -> None:
    result = ReadinessStabilityContract().evaluate(
        _report([_sample(503) for _ in range(10)])
    )

    assert (result.status, result.branch) == (Status.FAIL, "incorrect")


def test_sp002_identical_readiness_and_health_warns() -> None:
    samples = [_sample() for _ in range(10)]

    result = ReadinessStabilityContract().evaluate(
        _report(samples, health=_sample())
    )

    assert (result.status, result.branch) == (Status.WARN, "same_as_health")


def test_sp002_latency_budget_is_warning_not_failure() -> None:
    samples = [_sample(latency_ms=10) for _ in range(9)] + [
        _sample(latency_ms=55)
    ]

    result = ReadinessStabilityContract().evaluate(
        _report(samples, latency_budget="50ms")
    )

    assert (result.status, result.branch) == (Status.WARN, "latency_over_budget")
    assert result.actual["latency_p50_ms"] == 10
    assert result.actual["latency_max_ms"] == 55
    assert result.actual["n"] == 10


def test_sp002_stable_readiness_passes() -> None:
    result = ReadinessStabilityContract().evaluate(
        _report([_sample(latency_ms=index + 1) for index in range(10)])
    )

    assert (result.status, result.branch) == (Status.PASS, "stable")


#: The ten probe latencies service-b actually returned, in order. The first
#: sample is the target's own first-request cost, not connection setup: probing
#: the same warm container ten times on a fresh connection each time measured
#: 1.49ms at the median against 1.40ms over a shared one, so the 150x step
#: between sample one and the rest is a property of the application.
SERVICE_B_LATENCIES_MS = [
    89.255, 0.841, 0.655, 0.858, 0.538, 0.512, 0.64, 0.521, 0.552, 0.472,
]


def test_sp002_reports_a_sub_millisecond_p50_as_it_was_measured() -> None:
    """The row that read `p50 0ms, max 89ms` and looked like a broken tool.

    The median of these ten samples is 0.596ms. Every summary reached the
    formatter through `int()`, which truncates toward zero, so a latency no HTTP
    probe can return was printed next to a max taken from the same burst.
    """
    samples = [_sample(latency_ms=ms) for ms in SERVICE_B_LATENCIES_MS]

    result = ReadinessStabilityContract().evaluate(_report(samples))

    assert (result.status, result.branch) == (Status.PASS, "stable")
    assert result.actual["latency_p50_ms"] == pytest.approx(0.596)
    assert "p50 0.6ms" in result.summary
    assert "p50 0ms" not in result.summary
    assert "max 89ms" in result.summary


def test_sp002_keeps_the_sub_millisecond_p50_when_it_warns_too() -> None:
    """The over-budget branch prints the same pair and had the same defect."""
    samples = [_sample(latency_ms=ms) for ms in SERVICE_B_LATENCIES_MS]

    result = ReadinessStabilityContract().evaluate(
        _report(samples, latency_budget="50ms")
    )

    assert (result.status, result.branch) == (Status.WARN, "latency_over_budget")
    assert "p50 0.6ms" in result.summary
    assert "readiness max latency 89ms" in result.summary


def test_sp002_missing_baseline_is_an_engine_invariant() -> None:
    with pytest.raises(AssertionError, match="completed readiness baseline"):
        ReadinessStabilityContract().evaluate(_report(None))
