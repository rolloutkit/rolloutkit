"""SP004 — drain window for newly arriving connections."""

from __future__ import annotations

from collections import Counter
from typing import Any

from preflightkit.config.models import DrainStrategy
from preflightkit.contracts.base import (
    ACCEPT_WINDOW_MEASURED,
    DIRECT_CONNECTION_PATH,
    SHUTDOWN_BUDGET_RESOLVABLE,
    SHUTDOWN_STARTED,
    ContractResult,
    Status,
)
from preflightkit.engine.context import RunReport
from preflightkit.traffic.accept_probe import (
    ACCEPT_PROBE_INTERVAL_MS,
    AcceptAttempt,
    AcceptOutcome,
)

_THIN_MARGIN_RATIO = 0.20


class DrainWindowContract:
    id = "SP004"
    name = "drain-window"
    required = True
    PRECONDITIONS = (
        SHUTDOWN_STARTED,
        DIRECT_CONNECTION_PATH,
        SHUTDOWN_BUDGET_RESOLVABLE,
        ACCEPT_WINDOW_MEASURED,
    )

    #: The order the in_app clauses are asked in — and the only thing that
    #: decides the verdict when more than one of them holds, which is the normal
    #: case rather than the exception: a listener that closes early usually
    #: resets something on the way out, and a target that never signals
    #: readiness can do either while still failing to signal. Until this tuple
    #: existed the answer was whichever `if` came first in the source, which is
    #: an ordering nobody declared and no test could see reversed.
    #:
    #: Worst first, and worst means loss the caller could not have seen coming.
    #: `accept_then_reset` is a connection the client already believed it had,
    #: so it outranks a listener that closed early — a breach of a declared
    #: window, but one that arrives as a refusal the caller can retry. Both
    #: outrank the two advisories, which describe a drain that worked and could
    #: be observed or spared more room. Reporting an advisory over either
    #: failure would understate the run.
    #:
    #: The first branch only sees connections opened *inside* the declared
    #: window (see `_reset_classes`), which is what makes this order safe to
    #: keep. A reset that lands after the window is the kernel emptying an
    #: accept queue on close, and it happens on well-behaved targets; letting it
    #: outrank `in_app_covered` would have the strictest branch fire on the
    #: healthiest run.
    IN_APP_PRECEDENCE = (
        "accept_then_reset",
        "in_app_listener_closed_early",
        "in_app_readiness_not_signaled",
        "in_app_thin_margin",
        "in_app_covered",
    )

    BRANCHES = {
        "shutdown_never_started": Status.INCONCLUSIVE,
        "port_proxy_likely": Status.INCONCLUSIVE,
        "budget_below_teardown_floor": Status.INCONCLUSIVE,
        "accept_window_unmeasured": Status.INCONCLUSIVE,
        "accept_then_reset": Status.FAIL,
        "in_app_listener_closed_early": Status.FAIL,
        "in_app_readiness_not_signaled": Status.WARN,
        "in_app_thin_margin": Status.WARN,
        "in_app_covered": Status.PASS,
        "prestop_not_applicable": Status.PASS,
        "none_uncovered": Status.WARN,
    }

    def preconditions(self, report: RunReport):
        """Timing gates apply only when the application owns the drain window.

        ``none`` is already a useful warning from the declared profile, and
        ``prestop`` delegates the gap to the platform before T0. Neither verdict
        depends on post-SIGTERM listener timing or even an observed reaction.

        `ACCEPT_WINDOW_MEASURED` is dropped for the same reason, and under
        ``prestop`` dropping it is not a convenience: the probe is stopped at T0
        by design, so the last accepted connection is always on the wrong side
        of the signal and the gate would refuse every preStop run there is.
        """
        if report.config.deployment.drain.strategy in (
            DrainStrategy.NONE,
            DrainStrategy.PRESTOP,
        ):
            return tuple(
                condition
                for condition in self.PRECONDITIONS
                if condition not in (SHUTDOWN_STARTED, ACCEPT_WINDOW_MEASURED)
            )
        return self.PRECONDITIONS

    def evaluate(self, report: RunReport) -> ContractResult:
        strategy = report.config.deployment.drain.strategy
        required = report.config.deployment.drain.in_app_window
        accept_window = report.accept_window_ms
        evidence = _evidence(report)
        in_app = strategy is DrainStrategy.IN_APP
        inside, after = _reset_classes(report, required) if in_app else ([], [])
        actual = {
            "strategy": str(strategy),
            "accept_window_ms": accept_window,
            "readiness_drop_delay_ms": report.readiness_drop_delay_ms,
            "readiness_drop_mode": report.readiness_drop_observation,
            # The raw count stays what it always was: every post-T0 reset the
            # probe saw. The two below are the split the verdict is taken on,
            # and only in_app declares a window to split against.
            "accept_then_reset": len(report.accept_then_reset),
            "accept_then_reset_in_window": len(inside) if in_app else None,
            "accept_then_reset_after_window": len(after) if in_app else None,
        }

        def result(status: Status, summary: str, branch: str) -> ContractResult:
            notes = [
                f"accept_window is resolved to the {report.accept_probe_interval_ms}ms "
                "probe interval; it is not a continuous timestamp."
            ]
            if after:
                first = report.offset_ms(after[0].started_ns) or 0.0
                notes.append(
                    f"{len(after)} connection(s) were reset after the {required}ms "
                    f"window had already closed (first started at +{first:.0f}ms). "
                    "Closing a listening socket resets whatever the kernel has "
                    "already handshaken into its accept queue; the window those "
                    "connections were promised had elapsed, so they are evidence "
                    "and do not decide the verdict."
                )
            return ContractResult(
                self.id,
                self.name,
                status,
                summary,
                branch=branch,
                expected=_expected(report),
                actual=actual,
                evidence=evidence,
                notes=notes,
            )

        if strategy is DrainStrategy.PRESTOP:
            return result(
                Status.PASS,
                "not_applicable: preStop owns routing removal, so the new-connection "
                "probe stops at T0 and listener timing is evidence only",
                "prestop_not_applicable",
            )

        if strategy is DrainStrategy.NONE:
            return result(
                Status.WARN,
                "no drain mechanism covers routing propagation; production "
                "rollouts can send traffic to a process that is shutting down. "
                "Add a preStop sleep, or test application-owned draining with "
                "--drain in_app",
                "none_uncovered",
            )

        measured = accept_window if accept_window is not None else float("-inf")

        def accept_then_reset() -> tuple[Status, str] | None:
            if not inside:
                return None
            first = report.offset_ms(inside[0].started_ns)
            return Status.FAIL, (
                f"{len(inside)} connection(s) opened inside the {required}ms "
                f"window were reset without a response (first started at "
                f"+{(first or 0):.0f}ms)"
            )

        def in_app_listener_closed_early() -> tuple[Status, str] | None:
            if measured >= required:
                return None
            shown = accept_window if accept_window is not None else 0.0
            return Status.FAIL, (
                f"listener closed {shown:.0f}ms after T0, but must remain open "
                f"for {required}ms; the load balancer is still routing"
            )

        def in_app_readiness_not_signaled() -> tuple[Status, str] | None:
            observation = report.readiness_drop_observation
            if observation == "status_change":
                return None
            return Status.WARN, (
                f"listener covered the {required}ms in-app window, but readiness "
                f"did not publish a status change ({observation}); load balancers "
                "outside Kubernetes cannot observe the drain"
            )

        def in_app_thin_margin() -> tuple[Status, str] | None:
            reserve = measured - required
            if reserve >= required * _THIN_MARGIN_RATIO:
                return None
            return Status.WARN, (
                f"listener covered the {required}ms in-app window with only "
                f"{reserve:.0f}ms reserve"
            )

        def in_app_covered() -> tuple[Status, str] | None:
            return Status.PASS, (
                f"listener accepted new connections for {measured:.0f}ms after T0 "
                f"(required {required}ms)"
            )

        # Asked in the declared order, not in the order they are written. A
        # clause answers for the runs it recognises and returns None for the
        # rest; the last one answers unconditionally, so the loop always ends
        # in a verdict.
        clauses = {
            "accept_then_reset": accept_then_reset,
            "in_app_listener_closed_early": in_app_listener_closed_early,
            "in_app_readiness_not_signaled": in_app_readiness_not_signaled,
            "in_app_thin_margin": in_app_thin_margin,
            "in_app_covered": in_app_covered,
        }
        for branch in self.IN_APP_PRECEDENCE:
            verdict = clauses[branch]()
            if verdict is not None:
                return result(*verdict, branch)
        raise AssertionError(  # pragma: no cover - in_app_covered always answers
            "SP004 in_app precedence ended without a verdict"
        )


def _expected(report: RunReport) -> str:
    drain = report.config.deployment.drain
    if drain.strategy is DrainStrategy.IN_APP:
        return f"accept new connections for at least {drain.in_app_window}ms after T0"
    if drain.strategy is DrainStrategy.PRESTOP:
        return "preStop covers routing removal before SIGTERM"
    return "declare a drain mechanism"


def accept_window_cause(report: RunReport) -> str | None:
    """Why `accept_window_ms` is not a measurement, or None when it is.

    The window runs from T0 to the last connection the probe got accepted, so it
    only describes the drain if the probe was still being accepted when the
    signal landed. Up to one probe interval below zero says exactly that and
    nothing more: the probe samples every `accept_probe_interval_ms`, so a
    listener that closed at T0 leaves its last accept anywhere inside the
    interval before it, and the sign of the number is an artefact of where the
    sampling grid fell. Further back than one interval is a different statement:
    the probe was not getting connections accepted while the process was still
    running normally, so nothing it did after T0 was observed and the drain
    window was never measured at all. Why it was not — see
    `accept_window_unmeasured_reason`, which reads the interval the window is
    missing from rather than assuming one.

    Reporting that case as a listener that "closed -217ms after T0" is a
    stopwatch reading the tool does not have. It is the same refusal SP005's
    fallback makes: decline, and say what was and was not measured.
    """
    window = report.accept_window_ms
    if window is None:
        return "never_accepted"
    interval = report.accept_probe_interval_ms or ACCEPT_PROBE_INTERVAL_MS
    return None if window >= -interval else "last_accept_before_t0"


def accept_window_unmeasured_reason(report: RunReport, cause: str) -> str:
    """What to tell the reader, in the terms the evidence actually supports.

    One branch, and the mechanism named only when the evidence names it. What
    settles it is the interval the window is missing from — between the last
    accepted connection and T0 — and what the probe did inside it.

    Nothing inside it is the commonest case and the least obvious one: the probe
    is serial, so an attempt that connects and then waits out its response
    timeout blocks the next one. Under a saturated target that wait can span the
    signal, and then the probe was not being refused before T0, it was busy. The
    refusals that follow are from a socket already closed and say nothing about
    the interval, which is why they are not counted here.

    Where the probe did sample the interval, how those attempts failed separates
    the other two: a dropped SYN times out, which is a listening socket whose
    accept queue is full, and an RST is refused, which is no listening socket at
    all.
    """
    if cause == "never_accepted":
        return (
            "the accept probe never had a connection accepted, so there is no "
            "accept window to hold against the declared in-app window; check "
            "that target.port is the port the application listens on"
        )
    unanswered, refused = _attempts_before_t0_after_last_accept(report)
    mechanism = _unmeasured_mechanism(unanswered, refused)
    interval = report.accept_probe_interval_ms or ACCEPT_PROBE_INTERVAL_MS
    window = report.accept_window_ms or 0.0
    return (
        f"{_MECHANISM_PROSE[mechanism]} — accept window not measured: the last "
        f"connection the probe got accepted was {abs(window):.0f}ms before T0, "
        f"further back than the {interval}ms probe interval, so what the listener "
        "did after the signal was never observed. The raw accept_window_ms is in "
        "evidence"
    )


_MECHANISM_PROSE = {
    "probe_blocked": (
        "the probe was still waiting on its last accepted connection when the "
        "signal landed"
    ),
    "backlog_saturated": "probe saturated the backlog",
    "listener_gone": "the probe stopped being accepted before T0",
}


def _unmeasured_mechanism(unanswered: int, refused: int) -> str:
    if unanswered == 0 and refused == 0:
        return "probe_blocked"
    return "backlog_saturated" if unanswered > refused else "listener_gone"


def _attempts_before_t0_after_last_accept(report: RunReport) -> tuple[int, int]:
    """Attempts that sampled the missing interval, split by how they failed.

    Only the interval between the last accepted connection and T0 counts. An
    attempt after T0 is describing the shutdown, or the exit, which is the thing
    this window failed to measure — folding those in would let post-mortem
    refusals name a mechanism for a stretch of time they were never in.

    The two counts are the whole population of that interval: an attempt that
    connected would be the last accepted one instead, and every outcome other
    than these two sets `connected_ns`.
    """
    t2 = report.last_accepted_ns
    t0 = report.sigterm_ns
    if t2 is None or t0 is None:
        return 0, 0
    outcomes = [
        attempt.outcome
        for attempt in report.accept_attempts
        if t2 < attempt.started_ns < t0
    ]
    return (
        sum(1 for outcome in outcomes if outcome is AcceptOutcome.TIMEOUT),
        sum(1 for outcome in outcomes if outcome is AcceptOutcome.REFUSED),
    )


def accept_window_resolution_evidence(report: RunReport) -> dict[str, Any]:
    """The numbers the accept-window gate decided on, kept whatever it decided."""
    unanswered, refused = _attempts_before_t0_after_last_accept(report)
    cause = accept_window_cause(report)
    return {
        "accept_window_ms": report.accept_window_ms,
        "probe_interval_ms": report.accept_probe_interval_ms
        or ACCEPT_PROBE_INTERVAL_MS,
        "t2_last_accepted_offset_ms": report.offset_ms(report.last_accepted_ns),
        "unanswered_before_t0": unanswered,
        "refused_before_t0": refused,
        "cause": cause,
        "mechanism": (
            _unmeasured_mechanism(unanswered, refused)
            if cause == "last_accept_before_t0"
            else None
        ),
    }


def _evidence(report: RunReport) -> dict[str, Any]:
    t2 = report.last_accepted_ns
    outcomes = Counter(
        str(attempt.outcome)
        for attempt in report.accept_attempts
        if report.sigterm_ns is not None and attempt.started_ns >= report.sigterm_ns
    )
    after_t2 = Counter(
        str(attempt.outcome)
        for attempt in report.accept_attempts
        if t2 is not None and attempt.started_ns > t2
    )
    return {
        "probe_location": report.probe_location,
        "probe_fallback_reason": report.probe_fallback_reason,
        "t0_sigterm_ns": report.sigterm_ns,
        "t1_readiness_offset_ms": report.readiness_drop_delay_ms,
        "t1_readiness_mode": report.readiness_drop_observation,
        "t2_last_accepted_offset_ms": report.offset_ms(t2),
        "t4_exit_offset_ms": report.shutdown_duration_ms,
        "t4_exit_source": report.shutdown_duration_source,
        "t4_observed_exit_offset_ms": report.observed_shutdown_duration_ms,
        "accept_window_ms": report.accept_window_ms,
        "accept_window_cause": accept_window_cause(report),
        "accept_window_resolution_ms": report.accept_probe_interval_ms,
        "probe_interval_ms": report.accept_probe_interval_ms,
        "accept_probe_policy": (
            "stop_at_t0"
            if report.config.deployment.drain.strategy is DrainStrategy.PRESTOP
            else "continue_after_t0"
        ),
        "attempts_started_after_t0": sum(
            1
            for attempt in report.accept_attempts
            if report.sigterm_ns is not None and attempt.started_ns >= report.sigterm_ns
        ),
        "terminal_refused_streak": report.accept_refused_streak_target,
        "attempts_after_t0": dict(outcomes),
        "rejections_after_t2": dict(after_t2),
        # The window the events below are classified against, as a number. It
        # was previously recoverable only from the `expected` sentence, which
        # made the verdict unauditable from the report: a reader could see the
        # resets and not the boundary that decided which of them counted.
        "in_app_window_ms": (
            report.config.deployment.drain.in_app_window
            if report.config.deployment.drain.strategy is DrainStrategy.IN_APP
            else None
        ),
        "accept_then_reset": [
            {
                # The branch keys on when the connection was *requested*, so
                # that is the offset published. `connected_offset_ms` is when
                # the handshake finished, which on a loaded target trails the
                # request by as much as the accept lag and is the wrong side of
                # the boundary to judge by.
                "started_offset_ms": report.offset_ms(attempt.started_ns),
                "connected_offset_ms": report.offset_ms(attempt.connected_ns),
                "finished_offset_ms": report.offset_ms(attempt.finished_ns),
                "error": attempt.error,
            }
            for attempt in report.accept_then_reset
        ],
    }


def _reset_classes(
    report: RunReport, window_ms: float
) -> tuple[list[AcceptAttempt], list[AcceptAttempt]]:
    """Split post-T0 resets by whether the request began inside the window.

    A connection requested while the declared window was still open was
    promised an answer, and a reset instead of one is a defect. A connection
    requested after the window closed was promised nothing: the application has
    already served the interval it declared, and a rollout has removed the
    endpoint by then, so the only reason our probe is still knocking is that we
    keep the stream running to measure when the listener actually closed.
    Resetting that connection is what closing a listening socket does to
    whatever the kernel has already handshaken into its accept queue — a
    property of TCP, not of the application.

    The boundary is closed at the top: a window of 1200ms includes a request
    made at exactly +1200ms, because that is the last instant the declaration
    covers. An offset that cannot be computed at all is counted as inside —
    unreachable here, since a reset only qualifies once T0 exists, but the
    unclassifiable event belongs on the side that reports rather than the side
    that stays quiet.
    """
    inside: list[AcceptAttempt] = []
    after: list[AcceptAttempt] = []
    for attempt in report.accept_then_reset:
        started = report.offset_ms(attempt.started_ns)
        (after if started is not None and started > window_ms else inside).append(
            attempt
        )
    return inside, after
