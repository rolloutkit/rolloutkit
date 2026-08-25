"""SP006 — shutdown deadline."""

from __future__ import annotations

from preflightkit.config.duration import format_ms
from preflightkit.contracts.base import (
    SHUTDOWN_BUDGET_RESOLVABLE,
    ContractResult,
    Status,
)
from preflightkit.engine.context import RunReport

MARGIN_WARN_RATIO = 0.20


class DeadlineContract:
    id = "SP006"
    name = "shutdown-deadline"
    required = True
    PRECONDITIONS = (SHUTDOWN_BUDGET_RESOLVABLE,)

    BRANCHES = {
        "budget_below_teardown_floor": Status.INCONCLUSIVE,
        "past_deadline": Status.FAIL,
        "thin_margin": Status.WARN,
        "within_budget": Status.PASS,
    }

    def evaluate(self, report: RunReport) -> ContractResult:
        budget = report.config.deployment.shutdown_budget_ms
        duration = report.shutdown_duration_ms
        margin = None if duration is None else budget - duration
        expected = f"exit within {format_ms(budget)}"
        actual = {
            "shutdown_duration_ms": duration,
            "shutdown_budget_ms": budget,
            "margin_ms": margin,
            "sigkill_sent": report.sigkill_sent,
            "sigkill_effective": report.sigkill_effective,
            "teardown_calibration_status": report.teardown_calibration_status,
        }
        notes = [
            f"Budget is terminationGracePeriod ({format_ms(report.config.deployment.termination_grace_period)}) "
            f"minus preStop ({format_ms(report.config.deployment.pre_stop.duration)}). "
            "The hook is spent from the same window."
        ]

        assert duration is not None and margin is not None, (
            "SP006 is evaluated only after the daemon reports container exit"
        )

        # Two ways to blow the deadline, and both have to be caught here. The
        # obvious one is a measured overrun. The other is a process that only
        # stopped because SIGKILL ended it: its exit time is inside the budget,
        # but nothing about that exit was voluntary. Judging on the clock alone
        # reported WARN with a margin of -107.5ms on a real image — a FAIL the
        # tool let through.
        if margin < 0 or report.sigkill_effective:
            if report.sigkill_effective:
                summary = (
                    f"killed by SIGKILL at the end of the {format_ms(budget)} budget "
                    f"(exit {report.exit_code}); the process never shut itself down"
                )
            else:
                summary = (
                    f"exited in {format_ms(int(duration))}, "
                    f"{format_ms(int(-margin))} past the {format_ms(budget)} budget"
                )
            return ContractResult(
                self.id,
                self.name,
                Status.FAIL,
                summary,
                branch="past_deadline",
                expected=expected,
                actual=actual,
                evidence={"logs_tail": report.logs_tail[-1000:]},
                notes=notes
                + [
                    "In Kubernetes this is the point where every open connection "
                    "is severed, whatever is still running.",
                ],
            )

        if margin < budget * MARGIN_WARN_RATIO:
            return ContractResult(
                self.id,
                self.name,
                Status.WARN,
                f"exited in {format_ms(int(duration))}, leaving only "
                f"{format_ms(int(margin))} of the {format_ms(budget)} budget",
                branch="thin_margin",
                expected=expected,
                actual=actual,
                notes=notes
                + [
                    "A margin under "
                    f"{int(MARGIN_WARN_RATIO * 100)}% of the budget is one slow "
                    "dependency away from a SIGKILL in production.",
                ],
            )

        return ContractResult(
            self.id,
            self.name,
            Status.PASS,
            f"exited in {format_ms(int(duration))} of {format_ms(budget)}",
            branch="within_budget",
            expected=expected,
            actual=actual,
            notes=notes,
        )
