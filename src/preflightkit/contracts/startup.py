"""SP001 — startup."""

from __future__ import annotations

from preflightkit.config.duration import format_measured_ms, format_ms
from preflightkit.contracts.base import ContractResult, Status
from preflightkit.engine.context import RunReport


class StartupContract:
    id = "SP001"
    name = "startup"
    required = True
    PRECONDITIONS = ()

    BRANCHES = {
        "over_budget": Status.WARN,
        "within_resolution": Status.PASS,
        "within_budget": Status.PASS,
    }

    def evaluate(self, report: RunReport) -> ContractResult:
        budget = report.config.contracts.startup.budget
        duration = report.startup_duration_ms
        tcp_observed = (
            report.tcp_open_duration_ms
            if report.tcp_open_duration_ms is not None
            else _delta(report.container_started_ns, report.tcp_open_ns)
        )
        actual = {
            "container_start_overhead_ms": report.container_start_overhead_ms,
            "startup_resolution_ms": report.startup_resolution_ms,
            "tcp_open_status": (
                "MEASURED" if report.tcp_open_is_meaningful else "INCONCLUSIVE"
            ),
            "tcp_open_ms": tcp_observed if report.tcp_open_is_meaningful else None,
            "tcp_open_observed_ms": tcp_observed,
            "tcp_open_is_meaningful": report.tcp_open_is_meaningful,
            "readiness_ready_ms": duration,
            "readiness_status": report.readiness_status,
            "probe_location": report.probe_location,
            "probe_fallback_reason": report.probe_fallback_reason,
        }
        proxy_note = (
            [
                "The TCP timestamp was taken through a port proxy (Docker Desktop "
                "forwards published ports), so it measures the proxy, not the "
                "application. Readiness is the trustworthy startup signal here."
            ]
            if not report.tcp_open_is_meaningful
            else []
        )

        assert duration is not None, "SP001 is evaluated only after readiness passes"

        # Timing thresholds warn rather than fail: CI runners are noisy, and a
        # blocking check with false positives gets removed from the pipeline.
        overrun = duration - budget
        resolution = report.startup_resolution_ms
        if overrun > 0 and resolution is not None and overrun <= resolution:
            return ContractResult(
                self.id,
                self.name,
                Status.PASS,
                f"ready in {format_measured_ms(duration)}; the nominal "
                f"{format_measured_ms(overrun)} budget overrun is inside the "
                f"{format_measured_ms(resolution)} startup resolution",
                branch="within_resolution",
                expected=f"ready within {format_ms(budget)}",
                actual=actual,
                notes=proxy_note
                + [
                    "Startup budget differences below the measured resolution do not change the verdict."
                ],
            )

        if overrun > 0:
            return ContractResult(
                self.id,
                self.name,
                Status.WARN,
                f"ready in {format_measured_ms(duration)}, over the "
                f"{format_ms(budget)} budget",
                branch="over_budget",
                expected=f"ready within {format_ms(budget)}",
                actual=actual,
                notes=proxy_note
                + ["Duration thresholds warn by design; only binary facts fail."],
            )

        return ContractResult(
            self.id,
            self.name,
            Status.PASS,
            f"ready in {format_measured_ms(duration)} (budget {format_ms(budget)})",
            branch="within_budget",
            expected=f"ready within {format_ms(budget)}",
            actual=actual,
            notes=proxy_note,
        )


def _delta(start: int | None, end: int | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start) / 1_000_000
