"""The pre-experiment health check and the number derived from it."""

from __future__ import annotations

from rolloutkit.traffic.baseline import summarise
from rolloutkit.traffic.client import Outcome, RequestResult

MS = 1_000_000


def _result(request_id: int, status: int | None, duration_ms: float, outcome: Outcome) -> RequestResult:
    return RequestResult(
        request_id=request_id,
        outcome=outcome,
        status=status,
        started_ns=0,
        finished_ns=int(duration_ms * MS),
    )


def _burst(status: int, count: int = 25, duration_ms: float = 50) -> list[RequestResult]:
    return [_result(i, status, duration_ms, Outcome.COMPLETED) for i in range(count)]


def test_all_2xx_is_healthy() -> None:
    baseline = summarise(_burst(200))
    assert baseline.healthy
    assert baseline.succeeded == 25


def test_a_service_answering_500s_is_not_healthy() -> None:
    """The case that motivated the phase.

    A database with no schema answers every request with a 500, and every one of
    those arrives intact and on time. SP005 would have reported 200/200 completed
    and meant nothing by it.
    """
    baseline = summarise(_burst(500))
    assert not baseline.healthy
    assert baseline.completed == 25
    assert baseline.succeeded == 0
    assert "500" in baseline.describe()


def test_one_bad_sample_is_enough() -> None:
    """Strict on purpose: no load, no signal, a service that just said it was ready."""
    results = _burst(200, count=24) + [_result(99, 503, 50, Outcome.COMPLETED)]
    assert not summarise(results).healthy


def test_a_reset_is_not_a_success() -> None:
    results = _burst(200, count=24) + [
        _result(99, None, 10, Outcome.RESET_BEFORE_RESPONSE)
    ]
    baseline = summarise(results)
    assert not baseline.healthy
    assert baseline.outcomes["reset_before_response"] == 1


def test_an_empty_burst_is_never_healthy() -> None:
    assert not summarise([]).healthy


def test_sigterm_after_is_half_the_median() -> None:
    baseline = summarise(_burst(200, duration_ms=5000))
    assert baseline.p50_ms == 5000
    assert baseline.suggested_sigterm_after_ms == 2500


def test_a_sub_millisecond_service_still_gets_a_usable_window() -> None:
    """Rounding to 0 would race the request's own start."""
    baseline = summarise(_burst(200, duration_ms=0.4))
    assert baseline.suggested_sigterm_after_ms == 1


def test_percentiles_describe_the_spread() -> None:
    """p90 is nearest-rank, not interpolated.

    With ten samples the 90th percentile is the ninth, not the tenth — reporting
    the maximum as "p90" would hide the outlier the percentile exists to keep
    out of the median.
    """
    results = [_result(i, 200, float(i + 1), Outcome.COMPLETED) for i in range(10)]
    baseline = summarise(results)
    assert baseline.min_ms == 1
    assert baseline.max_ms == 10
    assert baseline.p50_ms == 5.5
    assert baseline.p90_ms == 9
