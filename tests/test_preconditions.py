"""Preconditions stop contracts from claiming verdicts over invalid windows."""

from __future__ import annotations

from rich.console import Console

from preflightkit.cli.main import FailOn, _blocking_results
from preflightkit.config.models import (
    Config,
    Contracts,
    Deployment,
    InflightContract as InflightConfig,
    InflightRequest,
    Target,
)
from preflightkit.contracts import ALL_CONTRACTS
from preflightkit.contracts.base import Status
from preflightkit.engine.context import RunReport
from preflightkit.engine.preconditions import evaluate_contracts
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.probes.http import ProbeResult
from preflightkit.reporters import json_out, terminal
from preflightkit.runtime.base import Pid1Facts, TeardownCalibration
from preflightkit.traffic.baseline import Baseline, ReadinessBaseline
from preflightkit.traffic.client import Outcome, RequestResult

SECOND = 1_000_000_000


def _calibration(*samples: float) -> TeardownCalibration:
    return TeardownCalibration(samples or (48.0, 49.0, 50.0, 51.0, 52.0))


def _config(*, budget: str = "30s") -> Config:
    return Config(
        target=Target(image="fixture:latest", port=8000),
        deployment=Deployment(termination_grace_period=budget),
        contracts=Contracts(
            inflight=InflightConfig(
                request=InflightRequest(path="/work", expected_duration="1s"),
                concurrent=1,
                sigterm_after="100ms",
            )
        ),
    )


def _baseline(status: int = 200, *, keep_alive: bool = True) -> Baseline:
    return Baseline(
        samples=1,
        completed=1,
        succeeded=1 if 200 <= status < 300 else 0,
        statuses={str(status): 1},
        outcomes={str(Outcome.COMPLETED): 1},
        durations_ms=[200.0],
        keep_alive_established=keep_alive,
    )


def _readiness_baseline() -> ReadinessBaseline:
    return ReadinessBaseline(
        samples=[
            ProbeResult(
                ok=True,
                status=200,
                latency_ns=10_000_000,
                headers={"content-type": "application/json"},
                body_head='{"ready": true}',
                body_head_bytes=15,
            )
            for _ in range(10)
        ]
    )


def _completed_request(*, connection_close: bool = False) -> RequestResult:
    return RequestResult(
        request_id=1,
        outcome=Outcome.COMPLETED,
        status=200,
        headers={
            "content-length": "2",
            **({"connection": "close"} if connection_close else {}),
        },
        started_ns=9 * SECOND,
        connected_ns=9 * SECOND,
        request_sent_ns=9 * SECOND,
        finished_ns=11 * SECOND,
    )


def _finished_report(*, budget: str = "30s", exit_code: int = 0) -> RunReport:
    report = RunReport(config=_config(budget=budget))
    report.container_started_ns = SECOND
    report.readiness_ok_ns = 2 * SECOND
    report.readiness_status = 200
    report.sigterm_ns = 10 * SECOND
    report.exit_ns = report.sigterm_ns + 500_000_000
    report.exit_code = exit_code
    report.teardown_floor_ms = 50.0
    report.teardown_calibration = _calibration()
    report.baseline = _baseline()
    report.readiness_baseline = _readiness_baseline()
    report.requests = [_completed_request()]
    return report


def _by_id(report: RunReport):
    return {result.id: result for result in evaluate_contracts(report, ALL_CONTRACTS)}


def test_django_shipped_config_cannot_pass_sp005() -> None:
    """Regression: shell PID 1 ignores SIGTERM while every request completes."""
    report = _finished_report(exit_code=137)
    report.pid1 = Pid1Facts(
        comm="sh", sig_caught=0x2, sig_ignored=0, sig_blocked=0
    )
    report.sigkill_sent = True
    report.sigkill_ns = report.sigterm_ns + 30 * SECOND
    report.exit_ns = report.sigkill_ns + 50_000_000

    results = _by_id(report)

    assert results["SP003"].facts["shutdown_started"] is False
    assert results["SP005"].status is Status.INCONCLUSIVE
    assert results["SP005"].branch == "shutdown_never_started"
    assert "shutdown never started" in results["SP005"].summary
    assert results["SP005"].actual["issued"] == 1
    assert "candidate_status" not in results["SP005"].actual
    assert results["SP005"].evidence["unresolved_candidate"] == {
        "status": "PASS",
        "branch": "all_completed",
    }
    assert "keepalive_closed_cleanly" in results["SP005"].evidence

    outcome = RunOutcome(report=report, results=list(results.values()))
    session = Session(run_id="pfk_test", image="fixture:latest", runs=[outcome])
    assert {r.id for r in _blocking_results(session, FailOn.ERROR, True)} == {
        "SP003",
        "SP006",
    }


def test_non_2xx_baseline_only_blocks_sp005() -> None:
    report = _finished_report()
    report.baseline = _baseline(500)

    results = _by_id(report)

    assert results["SP003"].status is Status.PASS
    assert results["SP005"].status is Status.INCONCLUSIVE
    assert results["SP005"].branch == "baseline_not_2xx"
    assert results["SP006"].status is Status.PASS


def test_budget_inside_measured_teardown_spread_blocks_sp006() -> None:
    report = _finished_report(budget="70ms")
    report.exit_ns = report.sigterm_ns + 60_000_000
    report.teardown_calibration = _calibration(40.0, 50.0, 60.0, 50.0, 50.0)
    report.teardown_floor_ms = report.teardown_calibration.floor_ms

    results = _by_id(report)

    assert results["SP006"].status is Status.INCONCLUSIVE
    assert results["SP006"].branch == "budget_below_teardown_floor"
    assert results["SP003"].status is Status.PASS
    calibration = results["SP006"].evidence["precondition"]["teardown_calibration"]
    assert calibration["floor_ms"] == 50.0
    assert calibration["stddev_ms"] > 0
    assert calibration["resolution_threshold_ms"] > 70


def test_unconfigured_sp005_is_skip_before_measurement_preconditions() -> None:
    report = RunReport(
        config=Config(target=Target(image="fixture:latest", port=8000))
    )
    report.container_started_ns = SECOND
    report.readiness_ok_ns = 2 * SECOND
    report.readiness_status = 200
    report.sigterm_ns = 10 * SECOND
    report.exit_ns = 11 * SECOND
    report.exit_code = 0
    report.teardown_floor_ms = 50.0
    report.readiness_baseline = _readiness_baseline()

    result = _by_id(report)["SP005"]

    assert (result.status, result.branch) == (Status.SKIP, "not_configured")
    assert "primary contract was not measured" in result.summary
    assert "--inflight-path" in result.summary


def test_none_drain_warns_even_when_shutdown_never_started() -> None:
    report = RunReport(
        config=Config(target=Target(image="fixture:latest", port=8000))
    )

    result = _by_id(report)["SP004"]

    assert (result.status, result.branch) == (Status.WARN, "none_uncovered")


def test_connection_close_is_not_applicable_without_steady_state_keepalive() -> None:
    report = _finished_report()
    report.baseline = _baseline(keep_alive=False)
    report.requests = [_completed_request(connection_close=True)]

    evidence = _by_id(report)["SP005"].evidence["keepalive_closed_cleanly"]

    assert evidence["status"] == "not_applicable"
    assert evidence["announced_connection_close"] is None
    assert "steady-state" in evidence["reason"]


def test_required_inconclusive_blocks_gating_unless_explicitly_allowed() -> None:
    report = _finished_report()
    report.container_start_overhead_ms = 752.0
    report.baseline = _baseline(500)
    outcome = RunOutcome(report=report, results=evaluate_contracts(report, ALL_CONTRACTS))
    session = Session(run_id="pfk_test", image="fixture:latest", runs=[outcome])
    console = Console(record=True, width=140)

    terminal.render(session, "test", console)
    document = json_out.build(session, "test")

    assert "1 required contract did not produce a verdict" in console.export_text()
    assert document["inconclusive"]["count"] == 1
    assert document["required_unmeasured"]["count"] == 1
    assert document["result"] != "INCONCLUSIVE"
    sp005_document = next(
        contract for contract in document["contracts"] if contract["id"] == "SP005"
    )
    assert "candidate_status" not in sp005_document["actual"]
    assert sp005_document["evidence"]["unresolved_candidate"]["status"] == "PASS"
    assert document["runs"][0]["container_start_overhead_ms"] == 752.0
    assert document["runs"][0]["startup_resolution_ms"] == 752.0
    assert [r.id for r in _blocking_results(session, FailOn.ERROR, False)] == [
        "SP005"
    ]
    assert _blocking_results(session, FailOn.ERROR, True) == []
    assert _blocking_results(session, FailOn.NONE, False) == []


def test_required_skip_blocks_gating_unless_explicitly_allowed() -> None:
    report = RunReport(
        config=Config(target=Target(image="fixture:latest", port=8000))
    )
    report.container_started_ns = SECOND
    report.readiness_ok_ns = 2 * SECOND
    report.readiness_status = 200
    report.sigterm_ns = 10 * SECOND
    report.exit_ns = 11 * SECOND
    report.exit_code = 0
    report.teardown_calibration = _calibration()
    report.teardown_floor_ms = report.teardown_calibration.floor_ms
    report.readiness_baseline = _readiness_baseline()
    outcome = RunOutcome(report=report, results=evaluate_contracts(report, ALL_CONTRACTS))
    session = Session(run_id="pfk_test", image="fixture:latest", runs=[outcome])

    sp005 = outcome.by_id()["SP005"]
    assert sp005.required is True
    assert sp005.status is Status.SKIP
    assert [r.id for r in _blocking_results(session, FailOn.ERROR, False)] == [
        "SP005"
    ]
    assert _blocking_results(session, FailOn.ERROR, True) == []
