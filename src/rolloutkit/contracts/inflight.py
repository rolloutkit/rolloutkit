"""SP005 — in-flight completion.

The product's central contract, and the only one whose meaning does not depend on
the drain strategy: whatever else happens, a request the server already accepted
must be finished.

There is no configurable threshold. 100% or FAIL.
"""

from __future__ import annotations

from typing import Any

from rolloutkit.contracts.base import (
    BASELINE_STEADY_STATE_2XX,
    INFLIGHT_ENABLED,
    READINESS_FALLBACK_RESOLVABLE,
    SHUTDOWN_STARTED,
    ContractResult,
    Status,
)
from rolloutkit.engine.context import RunReport
from rolloutkit.traffic.client import BROKEN_OUTCOMES, Outcome, RequestResult


class InflightContract:
    id = "SP005"
    name = "inflight-completion"
    required = True
    PRECONDITIONS = (
        INFLIGHT_ENABLED,
        READINESS_FALLBACK_RESOLVABLE,
        SHUTDOWN_STARTED,
        BASELINE_STEADY_STATE_2XX,
    )

    BRANCHES = {
        "disabled": Status.SKIP,
        "readiness_fallback_below_resolution": Status.INCONCLUSIVE,
        "shutdown_never_started": Status.INCONCLUSIVE,
        "baseline_not_2xx": Status.INCONCLUSIVE,
        "nothing_in_flight": Status.ERROR,
        "requests_destroyed": Status.FAIL,
        "all_completed": Status.PASS,
    }

    def evaluate(self, report: RunReport) -> ContractResult:
        assert report.config.contracts.inflight is not None
        if report.sigterm_ns is None:
            return ContractResult(
                self.id,
                self.name,
                Status.ERROR,
                "SIGTERM was not sent, so no in-flight window exists",
                branch="nothing_in_flight",
                expected="requests still running at the moment of SIGTERM",
                actual={
                    "inflight_target": report.inflight_target,
                    "path": report.inflight_path,
                    "issued": len(report.requests),
                    "in_flight_at_sigterm": 0,
                    "completed": 0,
                    "reset": 0,
                },
                evidence={
                    "completion": _completion_evidence(0, 0),
                    "window": _window_evidence(report),
                },
            )

        in_flight = [r for r in report.requests if _was_in_flight(r, report.sigterm_ns)]
        broken = [r for r in in_flight if r.outcome in BROKEN_OUTCOMES]
        completed = [r for r in in_flight if r.outcome is Outcome.COMPLETED]

        actual: dict[str, Any] = {
            "inflight_target": report.inflight_target,
            "path": report.inflight_path,
            "issued": len(report.requests),
            "in_flight_at_sigterm": len(in_flight),
            "completed": len(completed),
            "reset": len(broken),
        }
        evidence: dict[str, Any] = {
            "completion": _completion_evidence(len(completed), len(in_flight)),
            "broken_requests": [_describe(r, report) for r in broken],
            "keepalive_closed_cleanly": _keepalive_evidence(completed, report),
            "window": _window_evidence(report),
        }
        notes = _window_notes(report)

        # An experiment where nothing was in flight proves nothing. Reporting PASS
        # here would be the worst possible outcome: a green result that measured
        # an empty window.
        if not in_flight:
            return ContractResult(
                self.id,
                self.name,
                Status.ERROR,
                "no request was in flight when SIGTERM was sent — nothing was measured",
                expected="requests still running at the moment of SIGTERM",
                branch="nothing_in_flight",
                actual=actual,
                evidence=evidence,
                notes=notes + _closed_early_note(report),
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


def _completion_evidence(completed: int, in_flight: int) -> dict[str, Any]:
    return {
        "completed": completed,
        "in_flight_at_sigterm": in_flight,
        "completion_rate": (
            None if in_flight == 0 else round(completed / in_flight, 4)
        ),
    }


#: How much wider than the measurement noise the in-flight window has to be
#: before its boundary means anything. Measured on a real image: 1.4-1.8ms of
#: daemon jitter against a 30ms window is ~20x, and that window worked. Recorded
#: as evidence rather than enforced as a gate — the ratio explains a result, it
#: does not by itself invalidate one.
MIN_JITTER_RATIO = 10

#: The floor under the ratio. A window this short is not resolved on the
#: fallback path however clean the ratio looks.
#:
#: The ratio is a relative measure, so it can be cleared two ways: by a window
#: that is genuinely wide, or by a probe path that happened to be quiet. The
#: second is the case this catches. Measured across three conditions — an idle
#: macOS laptop, the same laptop under full CPU load, and a native Linux CI
#: daemon — the jitter floor moved by 3.4x between hosts (0.154ms against
#: 0.516ms median), and the same image at 1ms and 2ms of readiness delay
#: therefore resolved on the laptop and was refused on the runner. Nothing about
#: those services differed. See docs/field-notes.md, "Three hosts, pipeline cost,
#: and the fallback decision".
#:
#: Why 3ms and not 5. On the most conservative host measured, any floor in
#: (4.15, 6.18] would have reproduced its ratio verdicts exactly — but a floor
#: chosen from inside that band stops being a guard: for services with fast
#: readiness it becomes the deciding input, which is the thing the ratio exists
#: to avoid. Its job is the pathological case, not the ordinary one. Three
#: measured points is also a thin basis for a constant, so it errs low: at 3ms
#: the guard overturns two verdicts out of thirty across those conditions, and
#: both were verdicts the Linux runner had already refused on the ratio alone.
#:
#: It can only ever turn a yes into a no. A window the ratio already refuses is
#: not resolved by this passing, so raising it tightens the gate and lowering it
#: never loosens it past the ratio.
MIN_READINESS_WINDOW_MS = 3.0


def fallback_resolution_cause(
    p50_ms: float | None,
    jitter_ms: float | None,
    ratio: float | None,
) -> str | None:
    """Why the readiness fallback window cannot be resolved, or None if it can.

    One function because three callers ask the same question — the SP005
    precondition that publishes the verdict, and the two places in the lifecycle
    that decide whether to spend a baseline measuring a window nobody will be
    allowed to use. Three copies of a two-clause rule is how the copies come to
    disagree.

    The ratio is checked first and the floor second, so the reported cause names
    the ratio whenever the ratio is what refused. The floor never gets credit
    for a decision the ratio had already made.
    """
    if p50_ms is None or jitter_ms is None or jitter_ms <= 0 or ratio is None:
        return "unmeasured"
    if ratio < MIN_JITTER_RATIO:
        return "below_ratio"
    if p50_ms < MIN_READINESS_WINDOW_MS:
        return "below_window"
    return None


def _window_evidence(report: RunReport) -> dict[str, Any]:
    jitter = report.measurement_jitter_ms
    window = report.sigterm_after_ms
    ratio = None
    if jitter and window is not None:
        ratio = round(window / jitter, 1)
    return {
        "probe_location": report.probe_location,
        "probe_fallback_reason": report.probe_fallback_reason,
        "inflight_target": report.inflight_target,
        "path": report.inflight_path,
        "readiness_p50_ms": report.inflight_fallback_p50_ms,
        "readiness_jitter_ms": report.inflight_fallback_jitter_ms,
        "readiness_jitter_ratio": report.inflight_fallback_ratio,
        "sigterm_after_ms": window,
        "sigterm_after_source": report.sigterm_after_source,
        "measurement_jitter_ms": None if jitter is None else round(jitter, 3),
        "jitter_ratio": ratio,
        "baseline_p50_ms": None
        if report.baseline is None
        else _round(report.baseline.p50_ms),
    }


def _closed_early_note(report: RunReport) -> list[str]:
    """Explain an empty window only when the requests were actually issued.

    A precondition can refuse SP005 before the long-request phase runs at all,
    and the candidate verdict computed on that skipped phase still reaches the
    report as evidence. With no requests issued, "0 of 0 finished before the
    signal" is a count of an experiment that never happened — it reads as a
    second, competing cause next to the refusal that is the real one.
    """
    if not report.requests:
        return []
    finished_early = sum(
        1 for r in report.requests if (r.finished_ns or 0) <= report.sigterm_ns
    )
    return [
        f"{finished_early} of {len(report.requests)} requests finished "
        "before the signal. The window closed early: lower "
        "contracts.inflight.sigterm_after, or leave it unset and let it "
        "be derived from the baseline p50."
    ]


def _window_notes(report: RunReport) -> list[str]:
    """Say out loud whether the window is wide enough to be believed.

    The note is about a window the run actually opened, so it asks whether the
    run opened one. `sigterm_after_ms` is not that question: it is set inside
    the in-flight phase, which is the only reason a `None` check has held so
    far, and moving that assignment one line earlier — or setting it from a
    plan rather than from the phase — would have this note describing a window
    nothing was measured through, in a ratio against jitter that means nothing.
    """
    if not report.inflight_measurement_enabled:
        return []
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
