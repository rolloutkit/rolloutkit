"""SP002 — readiness correctness and steady-state stability."""

from __future__ import annotations

from typing import Any

from preflightkit.config.duration import format_ms
from preflightkit.contracts.base import ContractResult, Status
from preflightkit.engine.context import RunReport
from preflightkit.probes.http import ProbeResult
from preflightkit.traffic.baseline import READINESS_STABILITY_SAMPLES

_VOLATILE_COMPARISON_HEADERS = {
    "connection",
    "date",
    "keep-alive",
    "server",
    "transfer-encoding",
}


class ReadinessStabilityContract:
    id = "SP002"
    name = "readiness-stability"
    required = True
    PRECONDITIONS = ()

    BRANCHES = {
        "not_measured": Status.ERROR,
        "flapping": Status.FAIL,
        "incorrect": Status.FAIL,
        "same_as_health": Status.WARN,
        "latency_over_budget": Status.WARN,
        "stable": Status.PASS,
    }

    def evaluate(self, report: RunReport) -> ContractResult:
        baseline = report.readiness_baseline
        budget = report.config.contracts.readiness.latency_budget
        expected_status = report.config.probes.readiness.expected_status
        actual: dict[str, Any] = {
            "expected_status": expected_status,
            "latency_budget_ms": budget,
            "n": len(baseline.samples) if baseline is not None else 0,
            "latency_p50_ms": baseline.p50_ms if baseline is not None else None,
            "latency_max_ms": baseline.max_ms if baseline is not None else None,
        }
        evidence = baseline.as_dict() if baseline is not None else {}

        def result(status: Status, summary: str, branch: str) -> ContractResult:
            return ContractResult(
                self.id,
                self.name,
                status,
                summary,
                branch=branch,
                expected=(
                    f"{READINESS_STABILITY_SAMPLES} identical {expected_status} "
                    f"responses, max latency within {format_ms(budget)}"
                ),
                actual=actual,
                evidence=evidence,
            )

        if baseline is None or len(baseline.samples) != READINESS_STABILITY_SAMPLES:
            return result(
                Status.ERROR,
                f"readiness steady-state baseline needs "
                f"{READINESS_STABILITY_SAMPLES} samples, but measured "
                f"{actual['n']}",
                "not_measured",
            )

        outcomes = [_outcome(sample) for sample in baseline.samples]
        if len(set(outcomes)) > 1:
            shown = ", ".join(str(outcome) for outcome in outcomes)
            return result(
                Status.FAIL,
                f"readiness flapped across {len(outcomes)} sequential probes: {shown}",
                "flapping",
            )

        if not all(sample.ok for sample in baseline.samples):
            return result(
                Status.FAIL,
                f"readiness was stable but did not return expected status "
                f"{expected_status}: {outcomes[0]}",
                "incorrect",
            )

        health = baseline.health_sample
        if health is not None and _response_signature(health) == _response_signature(
            baseline.samples[0]
        ):
            return result(
                Status.WARN,
                "readiness and health return the same response; readiness does not "
                "appear to differ from liveness",
                "same_as_health",
            )

        maximum = baseline.max_ms
        if maximum is not None and maximum > budget:
            return result(
                Status.WARN,
                f"readiness max latency {format_ms(int(maximum))} exceeded the "
                f"{format_ms(budget)} budget (p50 {format_ms(int(baseline.p50_ms or 0))}, "
                f"n={len(baseline.samples)})",
                "latency_over_budget",
            )

        return result(
            Status.PASS,
            f"readiness returned {expected_status} for all {len(baseline.samples)} "
            f"probes (p50 {format_ms(int(baseline.p50_ms or 0))}, "
            f"max {format_ms(int(maximum or 0))})",
            "stable",
        )


def _outcome(sample: ProbeResult) -> int | str:
    if sample.status is not None:
        return sample.status
    if sample.error:
        return sample.error.split(":", 1)[0]
    return "no_response"


def _response_signature(sample: ProbeResult) -> tuple[object, ...]:
    headers = tuple(
        sorted(
            (name.lower(), value)
            for name, value in (sample.headers or {}).items()
            if name.lower() not in _VOLATILE_COMPARISON_HEADERS
        )
    )
    return sample.status, headers, sample.body_head
