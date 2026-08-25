"""SP004 — drain window for newly arriving connections."""

from __future__ import annotations

from collections import Counter
from typing import Any

from preflightkit.config.models import DrainStrategy
from preflightkit.contracts.base import (
    DIRECT_CONNECTION_PATH,
    IN_APP_WINDOW_RESOLVABLE,
    SHUTDOWN_BUDGET_RESOLVABLE,
    SHUTDOWN_STARTED,
    ContractResult,
    Status,
)
from preflightkit.engine.context import RunReport

_THIN_MARGIN_RATIO = 0.20


class DrainWindowContract:
    id = "SP004"
    name = "drain-window"
    required = True
    PRECONDITIONS = (
        SHUTDOWN_STARTED,
        DIRECT_CONNECTION_PATH,
        SHUTDOWN_BUDGET_RESOLVABLE,
        IN_APP_WINDOW_RESOLVABLE,
    )

    BRANCHES = {
        "shutdown_never_started": Status.INCONCLUSIVE,
        "port_proxy_likely": Status.INCONCLUSIVE,
        "budget_below_teardown_floor": Status.INCONCLUSIVE,
        "in_app_window_below_probe_resolution": Status.INCONCLUSIVE,
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
        """
        if report.config.deployment.drain.strategy in (
            DrainStrategy.NONE,
            DrainStrategy.PRESTOP,
        ):
            return tuple(
                condition
                for condition in self.PRECONDITIONS
                if condition is not SHUTDOWN_STARTED
            )
        return self.PRECONDITIONS

    def evaluate(self, report: RunReport) -> ContractResult:
        strategy = report.config.deployment.drain.strategy
        accept_window = report.accept_window_ms
        evidence = _evidence(report)
        actual = {
            "strategy": str(strategy),
            "accept_window_ms": accept_window,
            "readiness_drop_delay_ms": report.readiness_drop_delay_ms,
            "readiness_drop_mode": report.readiness_drop_observation,
            "accept_then_reset": len(report.accept_then_reset),
        }

        def result(status: Status, summary: str, branch: str) -> ContractResult:
            return ContractResult(
                self.id,
                self.name,
                status,
                summary,
                branch=branch,
                expected=_expected(report),
                actual=actual,
                evidence=evidence,
                notes=[
                    f"accept_window is resolved to the {report.accept_probe_interval_ms}ms "
                    "probe interval; it is not a continuous timestamp."
                ],
            )

        if strategy is DrainStrategy.PRESTOP:
            return result(
                Status.PASS,
                "not_applicable: preStop owns routing removal, so the new-connection "
                "probe stops at T0 and listener timing is evidence only",
                "prestop_not_applicable",
            )

        resets = report.accept_then_reset
        if resets:
            first = report.offset_ms(resets[0].connected_ns)
            return result(
                Status.FAIL,
                f"{len(resets)} connection(s) started after SIGTERM were reset "
                f"without a response (first connected at +{(first or 0):.0f}ms)",
                "accept_then_reset",
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

        required = report.config.deployment.drain.in_app_window
        measured = accept_window if accept_window is not None else float("-inf")
        if measured < required:
            shown = accept_window if accept_window is not None else 0.0
            return result(
                Status.FAIL,
                f"listener closed {shown:.0f}ms after T0, but must remain open "
                f"for {required}ms; the load balancer is still routing",
                "in_app_listener_closed_early",
            )

        if report.readiness_drop_observation != "status_change":
            observation = report.readiness_drop_observation
            return result(
                Status.WARN,
                f"listener covered the {required}ms in-app window, but readiness "
                f"did not publish a status change ({observation}); load balancers "
                "outside Kubernetes cannot observe the drain",
                "in_app_readiness_not_signaled",
            )

        reserve = measured - required
        if reserve < required * _THIN_MARGIN_RATIO:
            return result(
                Status.WARN,
                f"listener covered the {required}ms in-app window with only "
                f"{reserve:.0f}ms reserve",
                "in_app_thin_margin",
            )

        return result(
            Status.PASS,
            f"listener accepted new connections for {measured:.0f}ms after T0 "
            f"(required {required}ms)",
            "in_app_covered",
        )


def _expected(report: RunReport) -> str:
    drain = report.config.deployment.drain
    if drain.strategy is DrainStrategy.IN_APP:
        return f"accept new connections for at least {drain.in_app_window}ms after T0"
    if drain.strategy is DrainStrategy.PRESTOP:
        return "preStop covers routing removal before SIGTERM"
    return "declare a drain mechanism"


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
        "accept_then_reset": [
            {
                "connected_offset_ms": report.offset_ms(attempt.connected_ns),
                "finished_offset_ms": report.offset_ms(attempt.finished_ns),
                "error": attempt.error,
            }
            for attempt in report.accept_then_reset
        ],
    }
