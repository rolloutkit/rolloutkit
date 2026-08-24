"""SP005 — in-flight completion.

The product's central contract, and the only one whose meaning does not depend on
the drain strategy: whatever else happens, a request the server already accepted
must be finished.

There is no configurable threshold. 100% or FAIL.
"""

from __future__ import annotations

from typing import Any

from preflightkit.contracts.base import (
    BASELINE_STEADY_STATE_2XX,
    INFLIGHT_CONFIGURED,
    SHUTDOWN_STARTED,
    ContractResult,
    Status,
)
from preflightkit.engine.context import RunReport
from preflightkit.traffic.client import BROKEN_OUTCOMES, Outcome, RequestResult


class InflightContract:
    id = "SP005"
    name = "inflight-completion"
    required = True
    PRECONDITIONS = (
        INFLIGHT_CONFIGURED,
        SHUTDOWN_STARTED,
        BASELINE_STEADY_STATE_2XX,
    )

    BRANCHES = {
        "not_configured": Status.SKIP,
        "shutdown_never_started": Status.INCONCLUSIVE,
        "baseline_not_2xx": Status.INCONCLUSIVE,
        "nothing_in_flight": Status.ERROR,
        "requests_destroyed": Status.FAIL,
        "all_completed": Status.PASS,
    }

    def evaluate(self, report: RunReport) -> ContractResult:
        assert report.config.contracts.inflight is not None
        assert report.sigterm_ns is not None

        in_flight = [r for r in report.requests if _was_in_flight(r, report.sigterm_ns)]
        broken = [r for r in in_flight if r.outcome in BROKEN_OUTCOMES]
        completed = [r for r in in_flight if r.outcome is Outcome.COMPLETED]

        actual: dict[str, Any] = {
            "issued": len(report.requests),
            "in_flight_at_sigterm": len(in_flight),
            "completed": len(completed),
            "reset": len(broken),
        }
        evidence: dict[str, Any] = {
            "broken_requests": [_describe(r, report) for r in broken],
            "keepalive_closed_cleanly": _keepalive_evidence(completed, report),
            "window": _window_evidence(report),
        }
        notes = _window_notes(report)

        # An experiment where nothing was in flight proves nothing. Reporting PASS
        # here would be the worst possible outcome: a green result that measured
        # an empty window.
        if not in_flight:
            finished_early = sum(
                1 for r in report.requests if (r.finished_ns or 0) <= report.sigterm_ns
            )
            return ContractResult(
                self.id,
                self.name,
                Status.ERROR,
                "no request was in flight when SIGTERM was sent — nothing was measured",
                expected="requests still running at the moment of SIGTERM",
                branch="nothing_in_flight",
                actual=actual,
                evidence=evidence,
                notes=notes
                + [
                    f"{finished_early} of {len(report.requests)} requests finished "
                    "before the signal. The window closed early: lower "
                    "contracts.inflight.sigterm_after, or leave it unset and let it "
                    "be derived from the baseline p50.",
                ],
            )

        if broken:
            return ContractResult(
                self.id,
                self.name,
                Status.FAIL,
                f"{len(completed)}/{len(in_flight)} completed, {len(broken)} destroyed",
                branch="requests_destroyed",
                expected=f"{len(in_flight)}/{len(in_flight)} requests completed",
                actual=actual,
                evidence=evidence,
                notes=notes,
            )

        return ContractResult(
            self.id,
            self.name,
            Status.PASS,
            f"{len(completed)}/{len(in_flight)} in-flight requests completed",
            branch="all_completed",
            expected=f"{len(in_flight)}/{len(in_flight)} requests completed",
            actual=actual,
            evidence=evidence,
            notes=notes,
        )


#: How much wider than the measurement noise the in-flight window has to be
#: before its boundary means anything. Measured on a real image: 1.4-1.8ms of
#: daemon jitter against a 30ms window is ~20x, and that window worked. Recorded
#: as evidence rather than enforced as a gate — the ratio explains a result, it
#: does not by itself invalidate one.
MIN_JITTER_RATIO = 10


def _window_evidence(report: RunReport) -> dict[str, Any]:
    jitter = report.measurement_jitter_ms
    window = report.sigterm_after_ms
    ratio = None
    if jitter and window is not None:
        ratio = round(window / jitter, 1)
    return {
        "sigterm_after_ms": window,
        "sigterm_after_source": report.sigterm_after_source,
        "measurement_jitter_ms": None if jitter is None else round(jitter, 3),
        "jitter_ratio": ratio,
        "baseline_p50_ms": None
        if report.baseline is None
        else _round(report.baseline.p50_ms),
    }


def _window_notes(report: RunReport) -> list[str]:
    """Say out loud whether the window is wide enough to be believed."""
    jitter = report.measurement_jitter_ms
    window = report.sigterm_after_ms
    if not jitter or window is None:
        return []
    ratio = window / jitter
    if ratio >= MIN_JITTER_RATIO:
        return []
    return [
        f"The in-flight window is {window}ms against {jitter:.2f}ms of measurement "
        f"jitter ({ratio:.1f}x). Under {MIN_JITTER_RATIO}x, which request was in "
        "flight at T0 is partly a coin toss; treat the counts as approximate."
    ]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _was_in_flight(result: RequestResult, sigterm_ns: int) -> bool:
    """Accepted before the signal and still unfinished when it landed."""
    if result.connected_ns is None or result.connected_ns > sigterm_ns:
        return False
    return result.finished_ns is None or result.finished_ns > sigterm_ns


def _describe(result: RequestResult, report: RunReport) -> dict[str, Any]:
    return {
        "request_id": result.request_id,
        "outcome": str(result.outcome),
        "phase": str(result.phase),
        "status": result.status,
        "body_bytes": result.body_bytes,
        "expected_body_bytes": result.expected_body_bytes,
        "offset_ms": report.offset_ms(result.finished_ns),
        "detail": result.error_detail,
    }


def _keepalive_evidence(completed: list[RequestResult], report: RunReport) -> dict[str, Any]:
    """Did the server announce the close, or just vanish?

    A server winding down should send `Connection: close` on responses it finishes
    during shutdown rather than leaving a keep-alive connection to be torn down.
    """
    after_signal = [
        r
        for r in completed
        if report.sigterm_ns is not None and (r.finished_ns or 0) > report.sigterm_ns
    ]
    baseline_keep_alive = (
        report.baseline.keep_alive_established if report.baseline is not None else None
    )
    if baseline_keep_alive is not True:
        return {
            "status": "not_applicable",
            "precondition": "steady_state_keep_alive_established",
            "reason": "steady-state responses did not establish keep-alive",
            "baseline_keep_alive_established": baseline_keep_alive,
            "responses_after_sigterm": len(after_signal),
            "announced_connection_close": None,
        }
    return {
        "status": "applicable",
        "precondition": "steady_state_keep_alive_established",
        "baseline_keep_alive_established": baseline_keep_alive,
        "responses_after_sigterm": len(after_signal),
        "announced_connection_close": sum(1 for r in after_signal if r.connection_close),
    }
