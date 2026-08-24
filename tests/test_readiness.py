"""SP002 readiness stability verdicts and their retained evidence."""

from __future__ import annotations

from preflightkit.config.models import Config, Contracts, ReadinessContract, Target
from preflightkit.contracts.base import Status
from preflightkit.contracts.readiness import ReadinessStabilityContract
from preflightkit.engine.context import RunReport
from preflightkit.probes.http import ProbeResult
from preflightkit.traffic.baseline import ReadinessBaseline


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


def test_sp002_missing_baseline_is_an_error() -> None:
    result = ReadinessStabilityContract().evaluate(_report(None))

    assert (result.status, result.branch) == (Status.ERROR, "not_measured")
