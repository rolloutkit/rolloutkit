"""Machine-readable report."""

from __future__ import annotations

import json
from typing import Any

from preflightkit.contracts.base import Status
from preflightkit.contracts.inflight import MIN_JITTER_RATIO
from preflightkit.engine.context import RunReport
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.evidence.redact import Redactor
from preflightkit.provenance import preflightkit_commit

SCHEMA_VERSION = "1"


def build(session: Session, version: str) -> dict[str, Any]:
    if not session.runs:
        return {
            "version": SCHEMA_VERSION,
            "tool_version": version,
            "preflightkit_commit": preflightkit_commit(),
            "run_id": session.run_id,
            "target": {"image": session.image},
            "result": str(Status.ERROR),
            "error": session.infrastructure_error,
            "runs": [],
            "contracts": [],
        }

    last = session.runs[-1]
    config = last.report.config
    redactor = Redactor(config.secret_values())
    deployment = config.deployment

    document = {
        "version": SCHEMA_VERSION,
        "tool_version": version,
        "preflightkit_commit": preflightkit_commit(),
        "run_id": session.run_id,
        "target": {"image": session.image, "port": config.target.port},
        "profile": {
            "platform": str(deployment.platform),
            "termination_grace_period_ms": deployment.termination_grace_period,
            "pre_stop_ms": deployment.pre_stop.duration,
            "shutdown_budget_ms": deployment.shutdown_budget_ms,
            "drain_strategy": str(deployment.drain.strategy),
        },
        "result": str(session.verdict_status),
        "inconclusive": {
            "count": len(session.inconclusive),
            "contracts": [
                {"id": result.id, "reason": redactor.text(result.summary)}
                for result in session.inconclusive
            ],
        },
        "required_unmeasured": {
            "count": len(session.required_unmeasured),
            "contracts": [
                {
                    "id": result.id,
                    "status": str(result.status),
                    "reason": redactor.text(result.summary),
                }
                for result in session.required_unmeasured
            ],
        },
        "repeats": len(session.runs),
        "duration_ms": sum(r.duration_ms for r in session.runs),
        "phase_durations_ms": {
            phase: round(
                sum(run.report.phase_durations_ms.get(phase, 0.0) for run in session.runs),
                3,
            )
            for phase in last.report.phase_durations_ms
        },
        "environment": _environment(last.report),
        "baseline": _baseline(last.report),
        "readiness_baseline": (
            redactor.apply(last.report.readiness_baseline.as_dict())
            if last.report.readiness_baseline is not None
            else None
        ),
        "timeline": _timeline(last.report, redactor),
        "contracts": [
            {
                "id": r.id,
                "name": r.name,
                "status": str(r.status),
                "required": r.required,
                # Which code path produced the verdict. `fixtures/matrix.yaml`
                # asserts on this, so a contract that reaches the right status by
                # the wrong route is a test failure rather than a green run.
                "branch": r.branch,
                "summary": redactor.text(r.summary),
                "expected": r.expected,
                "actual": redactor.apply(r.actual),
                "evidence": redactor.apply(r.evidence),
                "notes": [redactor.text(n) for n in r.notes],
            }
            for r in session.aggregated
        ],
        "runs": [_run_summary(r, redactor) for r in session.runs],
    }

    spread = session.timing_spread(lambda report: report.shutdown_duration_ms)
    if spread is not None:
        document["shutdown_duration_ms_spread"] = spread
    startup_spread = session.timing_spread(lambda report: report.startup_duration_ms)
    if startup_spread is not None:
        document["startup_duration_ms_spread"] = startup_spread
    return document


def _environment(report: RunReport) -> dict[str, Any]:
    return {
        "host_id": report.host_id,
        "host_os": report.host_os,
        "cpu_count": report.cpu_count,
        "load_average": list(report.load_average) if report.load_average else None,
        "docker_endpoint": report.docker_endpoint,
        "docker_server": report.docker_server,
        "measurement_jitter_ms": report.measurement_jitter_ms,
        "probe_location": report.probe_location,
        "probe_fallback_reason": report.probe_fallback_reason,
        "probe_image": report.probe_image,
        "probe_clock_alignment_ms": report.probe_clock_alignment_ms,
        "port_proxy_likely": report.port_proxy_likely,
        "network_name": report.network_name,
        "traffic_endpoint": report.traffic_endpoint,
        "measurement_note": (
            "Timestamps are sourced from monotonic_ns. Application-facing "
            "observations are bounded by measurement_jitter_ms measured at "
            f"{report.probe_location}."
        ),
        "docker_init_injected": False,
    }


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _resolution_calibration(report: RunReport) -> dict[str, Any]:
    """What this host could resolve during this run.

    `MIN_JITTER_RATIO` cannot be chosen from one laptop. The quantity it guards
    — readiness p50 over probe-path jitter — is a property of the machine, and
    the spread only becomes visible once the same block exists for a Linux
    server, a macOS laptop and a CI runner. So it is written on every run rather
    than being derived once from a measurement campaign: the campaign is every
    run anyone has already made.

    It is recorded on both in-flight paths, not only the fallback. A run that
    points `--inflight-path` at a slower endpoint still measures the readiness
    baseline, and its ratio is the same evidence about the same host — the
    recommended workaround must not silently stop contributing data.

    `ratio` is therefore what the fallback path *would* have had to work with,
    not a verdict; a configured run reaching it is not a warning. The verdict
    lives in SP005's precondition, which reads the same numbers.

    The teardown floor is the other half of the same question and is already
    recorded per run in `teardown_calibration`.
    """
    readiness = report.readiness_baseline
    p50 = readiness.p50_ms if readiness is not None else None
    jitter = report.measurement_jitter_ms
    return {
        "host_id": report.host_id,
        # Here rather than in `environment`: this is a fact about the run. A
        # jitter figure measured under load is a different figure, and without
        # the load beside it the two are indistinguishable after the fact.
        "load_average": list(report.load_average) if report.load_average else None,
        "measurement_jitter_ms": _round(jitter),
        "measurement_jitter_source": report.probe_location,
        "measurement_jitter_samples": len(report.ping_latencies_ns),
        "readiness_p50_ms": _round(p50),
        "readiness_max_ms": _round(readiness.max_ms) if readiness is not None else None,
        "readiness_samples": len(readiness.samples) if readiness is not None else 0,
        "ratio": _round(p50 / jitter) if p50 is not None and jitter else None,
        "minimum_ratio": MIN_JITTER_RATIO,
        "inflight_target": report.inflight_target,
    }


def _baseline(report: RunReport) -> dict[str, Any] | None:
    """The pre-signal health check, and where `sigterm_after` came from."""
    if report.baseline is None:
        return None
    return {
        **report.baseline.as_dict(),
        "inflight_target": report.inflight_target,
        "path": report.inflight_path,
        "sigterm_after_ms": report.sigterm_after_ms,
        "sigterm_after_source": report.sigterm_after_source,
    }


def _timeline(report: RunReport, redactor: Redactor) -> list[dict[str, Any]]:
    origin = report.sigterm_ns or report.container_started_ns
    if origin is None:
        return []
    return [
        {
            "event": event.kind,
            "offset_ms": round(event.offset_ms_from(origin), 3),
            **redactor.apply(event.data),
        }
        for event in report.bus.ordered()
    ]


def _run_summary(run: RunOutcome, redactor: Redactor) -> dict[str, Any]:
    report = run.report
    return {
        "duration_ms": run.duration_ms,
        "phase_durations_ms": {
            phase: round(duration, 3)
            for phase, duration in report.phase_durations_ms.items()
        },
        "resolution_calibration": _resolution_calibration(report),
        "container_start_overhead_ms": report.container_start_overhead_ms,
        "startup_resolution_ms": report.startup_resolution_ms,
        "startup_duration_ms": report.startup_duration_ms,
        "shutdown_duration_ms": report.shutdown_duration_ms,
        # Which clock produced the number above, and what the other one said.
        # The daemon's own kill/die pair has no round trip of ours in it; the
        # floor is what this host charges to report a death that was instant.
        "shutdown_duration_source": report.shutdown_duration_source,
        "observed_shutdown_duration_ms": report.observed_shutdown_duration_ms,
        "daemon_shutdown_duration_ms": report.daemon_shutdown_duration_ms,
        "teardown_floor_ms": report.teardown_floor_ms,
        "teardown_calibration": report.teardown_calibration.as_dict()
        if report.teardown_calibration is not None
        else None,
        "teardown_calibration_status": report.teardown_calibration_status,
        "readiness_drop_delay_ms": report.readiness_drop_delay_ms,
        "readiness_drop_mode": report.readiness_drop_observation,
        "accept_window_ms": report.accept_window_ms,
        "accept_window_resolution_ms": report.accept_probe_interval_ms,
        "last_accepted_offset_ms": report.offset_ms(report.last_accepted_ns),
        "exit_code": report.exit_code,
        "sigkill_sent": report.sigkill_sent,
        "sigkill_effective": report.sigkill_effective,
        "pid1": report.pid1.as_dict() if report.pid1 else None,
        "runtime_handler_installed": report.runtime_handler_installed,
        "contracts": {r.id: str(r.status) for r in run.results},
    }


def dump(session: Session, version: str) -> str:
    return json.dumps(build(session, version), indent=2, sort_keys=False)
