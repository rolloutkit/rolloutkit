"""Resolve contract preconditions from facts measured during one run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from preflightkit.contracts.base import Contract, ContractResult, Precondition, Status
from preflightkit.config.models import DrainStrategy
from preflightkit.engine.context import RunReport
from preflightkit.engine.lifecycle import TEARDOWN_CALIBRATION_MAX_BUDGET_MS
from preflightkit.contracts.inflight import (
    MIN_JITTER_RATIO,
    MIN_READINESS_WINDOW_MS,
    fallback_resolution_cause,
)


@dataclass(frozen=True, slots=True)
class Resolution:
    satisfied: bool
    reason: str = ""
    evidence: dict[str, object] | None = None


Resolver = Callable[[RunReport], Resolution]


def _inflight_enabled(report: RunReport) -> Resolution:
    enabled = report.config.contracts.inflight is not None
    return Resolution(
        enabled,
        "SP005 was explicitly disabled by contracts.inflight: null",
    )


def _readiness_fallback_resolvable(report: RunReport) -> Resolution:
    """Two clauses, one branch.

    The ratio is the decision: can this host tell the window apart from its own
    noise. `MIN_READINESS_WINDOW_MS` is a guard behind it, for the window that
    clears the ratio only because the probe path was quiet. Both refusals reach
    the same verdict — SP005 declines and says to pass --inflight-path — so they
    are one branch, and `cause` in the evidence says which clause spoke. A
    second branch name here would be an exception list with one entry in it.
    """
    if report.inflight_target != "readiness_fallback":
        return Resolution(True)
    p50 = report.inflight_fallback_p50_ms
    jitter = report.inflight_fallback_jitter_ms
    ratio = report.inflight_fallback_ratio
    cause = fallback_resolution_cause(p50, jitter, ratio)
    evidence = {
        "inflight_target": report.inflight_target,
        "readiness_p50_ms": p50,
        "jitter_ms": jitter,
        "ratio": ratio,
        "minimum_ratio": MIN_JITTER_RATIO,
        "minimum_readiness_window_ms": MIN_READINESS_WINDOW_MS,
        "cause": cause,
    }
    if cause == "unmeasured":
        return Resolution(
            False,
            "readiness fallback cannot be resolved because readiness p50 or "
            "probe-path jitter was not measured; point --inflight-path at a slower "
            "endpoint",
            evidence,
        )
    if cause == "below_window":
        # Reported with the ratio it passed, because without it this reads as a
        # duplicate of the clause above rather than the case it actually is.
        return Resolution(
            False,
            f"readiness p50 {p50:.1f}ms is under the {MIN_READINESS_WINDOW_MS:.0f}ms "
            f"floor for a fallback window; the {ratio:.1f}x ratio clears "
            f"{MIN_JITTER_RATIO}x only because the probe path was quiet "
            f"({jitter:.2f}ms), and a window that short resolves on one host and "
            "not on another — point --inflight-path at a slower endpoint",
            evidence,
        )
    return Resolution(
        cause is None,
        f"readiness p50 {p50:.1f}ms, jitter {jitter:.1f}ms, ratio {ratio:.1f}x "
        f"is below the required {MIN_JITTER_RATIO}x; the in-flight window cannot "
        "be distinguished from measurement noise — point --inflight-path at a "
        "slower endpoint",
        evidence,
    )


def _shutdown_started(report: RunReport) -> Resolution:
    started = report.facts.get("shutdown_started") is True
    return Resolution(
        started,
        "shutdown never started: no readiness change, stopped accepts, or "
        "voluntary process exit was observed after SIGTERM",
        {
            "shutdown_started": started,
            "readiness_changed": report.readiness_drop_ns is not None
            and (report.sigkill_ns is None or report.readiness_drop_ns < report.sigkill_ns),
            "accept_stopped": report.accept_stopped_ns is not None,
            "process_exited_voluntarily": report.exit_ns is not None
            and not report.sigkill_effective,
        },
    )


def _baseline_steady_state_2xx(report: RunReport) -> Resolution:
    baseline = report.baseline
    healthy = baseline is not None and baseline.healthy
    reason = "baseline steady-state responses were not measured"
    evidence: dict[str, object] = {"baseline": None}
    if baseline is not None:
        reason = (
            "baseline steady-state responses were not all 2xx: "
            f"{baseline.describe()}"
        )
        evidence = {"baseline": baseline.as_dict()}
    return Resolution(healthy, reason, evidence)


def _shutdown_budget_resolvable(report: RunReport) -> Resolution:
    budget = report.config.deployment.shutdown_budget_ms
    if budget > TEARDOWN_CALIBRATION_MAX_BUDGET_MS:
        return Resolution(
            True,
            evidence={
                "shutdown_budget_ms": budget,
                "teardown_calibration": None,
                "teardown_calibration_status": "not_calibrated",
                "calibration_cutoff_ms": TEARDOWN_CALIBRATION_MAX_BUDGET_MS,
                "minimum_resolvable_budget_ms": None,
            },
        )
    calibration = report.teardown_calibration
    floor = calibration.floor_ms if calibration is not None else None
    threshold = (
        calibration.resolution_threshold_ms if calibration is not None else None
    )
    satisfied = threshold is not None and budget > threshold
    if floor is None:
        reason = (
            "teardown spread was not measured, so the shutdown budget is not "
            "resolvable"
        )
    else:
        reason = (
            f"shutdown budget {budget}ms is not greater than the measured "
            f"resolution threshold {threshold:.1f}ms (median floor {floor:.1f}ms "
            f"+ {calibration.stddev_k:g} x {calibration.stddev_ms:.1f}ms stddev)"
        )
    return Resolution(
        satisfied,
        reason,
        {
            "shutdown_budget_ms": budget,
            "teardown_calibration": calibration.as_dict()
            if calibration is not None
            else None,
            "teardown_calibration_status": report.teardown_calibration_status,
            "calibration_cutoff_ms": TEARDOWN_CALIBRATION_MAX_BUDGET_MS,
            "minimum_resolvable_budget_ms": threshold,
        },
    )


def _direct_connection_path(report: RunReport) -> Resolution:
    # PRESTOP does not inspect the post-T0 listener, and NONE is already a WARN
    # by declaration. A desktop port proxy cannot make either fact unknown.
    strategy = report.config.deployment.drain.strategy
    if strategy in (DrainStrategy.PRESTOP, DrainStrategy.NONE):
        return Resolution(True)
    return Resolution(
        not report.port_proxy_likely,
        "the published-port proxy accepts connections on behalf of the "
        "container, so listener timing does not describe the application",
        {
            "port_proxy_likely": report.port_proxy_likely,
            "traffic_endpoint": report.traffic_endpoint,
        },
    )


RESOLVERS: dict[str, Resolver] = {
    "inflight_enabled": _inflight_enabled,
    "readiness_fallback_resolvable": _readiness_fallback_resolvable,
    "shutdown_started": _shutdown_started,
    "baseline_steady_state_2xx": _baseline_steady_state_2xx,
    "shutdown_budget_resolvable": _shutdown_budget_resolvable,
    "direct_connection_path": _direct_connection_path,
}


def evaluate_contracts(report: RunReport, contracts: tuple[Contract, ...]) -> list[ContractResult]:
    """Resolve declarations, evaluate eligible contracts, and publish facts."""
    results: list[ContractResult] = []
    for contract in contracts:
        unmet = _first_unmet(contract, report)
        if unmet is not None and unmet[0].unmet_status is Status.SKIP:
            result = _blocked_result(contract, *unmet)
        else:
            candidate = contract.evaluate(report)
            result = (
                _blocked_result(contract, *unmet, candidate=candidate)
                if unmet is not None
                else candidate
            )
        result.required = contract.required
        report.facts.update(result.facts)
        results.append(result)
    return results


def _first_unmet(
    contract: Contract, report: RunReport
) -> tuple[Precondition, Resolution] | None:
    resolve = getattr(contract, "preconditions", None)
    preconditions = resolve(report) if resolve is not None else contract.PRECONDITIONS
    for precondition in preconditions:
        resolution = RESOLVERS[precondition.id](report)
        if not resolution.satisfied:
            return precondition, resolution
    return None


def _blocked_result(
    contract: Contract,
    precondition: Precondition,
    resolution: Resolution,
    *,
    candidate: ContractResult | None = None,
) -> ContractResult:
    candidate_evidence = candidate.evidence if candidate is not None else {}
    return ContractResult(
        id=contract.id,
        name=contract.name,
        status=precondition.unmet_status,
        summary=resolution.reason,
        branch=precondition.branch,
        expected=f"precondition {precondition.id}",
        actual={
            **(candidate.actual if candidate is not None else {}),
            "precondition": precondition.id,
            "satisfied": False,
        },
        evidence={
            **candidate_evidence,
            "precondition": resolution.evidence or {},
            "unresolved_candidate": (
                {
                    "status": str(candidate.status),
                    "branch": candidate.branch,
                }
                if candidate is not None
                else None
            ),
        },
        notes=(candidate.notes if candidate is not None else [])
        + (
            [
                "The experiment and traffic measurement completed, but this "
                "unresolved candidate was not published because its precondition "
                "did not hold."
            ]
            if candidate is not None
            else []
        ),
        facts=candidate.facts if candidate is not None else {},
        required=contract.required,
    )
