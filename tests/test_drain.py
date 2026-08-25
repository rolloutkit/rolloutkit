"""SP004 verdict table, one explicit test per branch."""

from __future__ import annotations

from rich.console import Console

from preflightkit.config.models import Config, Deployment, Drain, DrainStrategy, Target
from preflightkit.contracts.base import Status
from preflightkit.contracts.drain import DrainWindowContract
from preflightkit.engine.context import RunReport
from preflightkit.engine.preconditions import evaluate_contracts
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.reporters import terminal
from preflightkit.runtime.base import DaemonEvent, TeardownCalibration
from preflightkit.traffic.accept_probe import AcceptAttempt, AcceptOutcome

SECOND = 1_000_000_000


def _report(
    strategy: DrainStrategy,
    *,
    window_ms: int = 1200,
    accept_ms: float = 1600,
    readiness: str | None = "status_change",
    shutdown_ms: float = 500,
    reset: bool = False,
) -> RunReport:
    drain = (
        Drain(strategy=strategy, in_app_window=f"{window_ms}ms")
        if strategy is DrainStrategy.IN_APP
        else Drain(strategy=strategy)
    )
    report = RunReport(
        config=Config(
            target=Target(image="fixture:test", port=8000),
            deployment=Deployment(
                termination_grace_period="30s",
                drain=drain,
            ),
        )
    )
    report.sigterm_ns = 10 * SECOND
    report.exit_ns = report.sigterm_ns + int(shutdown_ms * 1_000_000)
    report.exit_code = 0
    report.accept_probe_interval_ms = 50
    report.accept_refused_streak_target = 3
    connected = report.sigterm_ns + int(accept_ms * 1_000_000)
    report.accept_attempts = [
        AcceptAttempt(
            started_ns=connected - 1_000_000,
            connected_ns=connected,
            finished_ns=connected + 1_000_000,
            outcome=AcceptOutcome.RESET if reset else AcceptOutcome.RESPONSE,
            error="ECONNRESET" if reset else None,
        )
    ]
    if readiness is not None:
        report.readiness_drop_ns = report.sigterm_ns + 100_000_000
        report.readiness_drop_mode = readiness
    return report


def _result(report: RunReport):
    return DrainWindowContract().evaluate(report)


def _resolved(report: RunReport):
    return evaluate_contracts(report, (DrainWindowContract(),))[0]


def test_in_app_listener_closed_early_fails() -> None:
    result = _result(_report(DrainStrategy.IN_APP, accept_ms=700))
    assert (result.status, result.branch) == (
        Status.FAIL,
        "in_app_listener_closed_early",
    )


def test_in_app_window_with_healthy_reserve_passes() -> None:
    result = _result(_report(DrainStrategy.IN_APP, accept_ms=1600))
    assert (result.status, result.branch) == (Status.PASS, "in_app_covered")


def test_in_app_window_with_less_than_twenty_percent_reserve_warns() -> None:
    result = _result(_report(DrainStrategy.IN_APP, accept_ms=1300))
    assert (result.status, result.branch) == (Status.WARN, "in_app_thin_margin")


def test_in_app_readiness_never_changed_warns() -> None:
    result = _result(_report(DrainStrategy.IN_APP, readiness=None))
    assert (result.status, result.branch) == (
        Status.WARN,
        "in_app_readiness_not_signaled",
    )
    assert result.actual["readiness_drop_mode"] == "never"


def test_in_app_readiness_becoming_unreachable_is_not_a_drain_signal() -> None:
    result = _result(_report(DrainStrategy.IN_APP, readiness="unreachable"))
    assert (result.status, result.branch) == (
        Status.WARN,
        "in_app_readiness_not_signaled",
    )


def test_prestop_accept_window_is_evidence_not_a_failure() -> None:
    result = _result(_report(DrainStrategy.PRESTOP, accept_ms=0))
    assert (result.status, result.branch) == (
        Status.PASS,
        "prestop_not_applicable",
    )
    assert result.summary.startswith("not_applicable:")
    assert result.evidence["accept_window_ms"] == 0
    assert result.evidence["accept_probe_policy"] == "stop_at_t0"


def test_prestop_near_shutdown_deadline_remains_not_applicable() -> None:
    result = _result(_report(DrainStrategy.PRESTOP, shutdown_ms=25_000, accept_ms=0))
    assert (result.status, result.branch) == (
        Status.PASS,
        "prestop_not_applicable",
    )


def test_none_always_warns() -> None:
    result = _result(_report(DrainStrategy.NONE))
    assert (result.status, result.branch) == (Status.WARN, "none_uncovered")


def test_accept_then_reset_fails_for_in_app_and_none() -> None:
    for strategy in (DrainStrategy.IN_APP, DrainStrategy.NONE):
        result = _result(_report(strategy, reset=True))
        assert (result.status, result.branch) == (Status.FAIL, "accept_then_reset")


def test_accept_then_reset_is_not_applicable_for_prestop() -> None:
    result = _result(_report(DrainStrategy.PRESTOP, accept_ms=100, reset=True))
    assert (result.status, result.branch) == (
        Status.PASS,
        "prestop_not_applicable",
    )


def test_connection_attempt_started_before_t0_is_not_sp004_reset() -> None:
    report = _report(DrainStrategy.IN_APP, accept_ms=0, reset=True)
    assert report.accept_attempts[0].started_ns < report.sigterm_ns
    assert report.accept_attempts[0].connected_ns == report.sigterm_ns
    assert report.accept_then_reset == []


def test_timeline_and_resolution_are_always_evidence() -> None:
    report = _report(DrainStrategy.IN_APP)
    report.daemon_events = [
        DaemonEvent("kill", daemon_ns=1_000_000_000, observed_ns=0),
        DaemonEvent("die", daemon_ns=1_450_000_000, observed_ns=0),
    ]
    result = _result(report)
    assert result.evidence["t0_sigterm_ns"] == 10 * SECOND
    assert result.evidence["t1_readiness_offset_ms"] == 100
    assert result.evidence["t1_readiness_mode"] == "status_change"
    assert result.evidence["t2_last_accepted_offset_ms"] == 1600
    assert result.evidence["t4_exit_offset_ms"] == 450
    assert result.evidence["t4_exit_source"] == "daemon_events"
    assert result.evidence["t4_observed_exit_offset_ms"] == 500
    assert result.evidence["accept_window_resolution_ms"] == 50


def test_terminal_prints_accept_window_with_its_resolution() -> None:
    report = _report(DrainStrategy.IN_APP)
    outcome = RunOutcome(report=report, results=[_result(report)])
    session = Session(run_id="pfk_test", image="fixture:test", runs=[outcome])
    console = Console(record=True, width=140)

    terminal.render(session, "test", console)

    assert "+1600ms (±50ms)" in console.export_text()


def test_sp004_shutdown_precondition_is_inconclusive() -> None:
    result = _resolved(_report(DrainStrategy.IN_APP))
    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "shutdown_never_started",
    )


def test_sp004_proxy_precondition_is_inconclusive() -> None:
    report = _report(DrainStrategy.IN_APP)
    report.facts["shutdown_started"] = True
    report.port_proxy_likely = True
    result = _resolved(report)
    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "port_proxy_likely",
    )


def test_sp004_budget_precondition_is_inconclusive() -> None:
    report = _report(DrainStrategy.NONE)
    report.config = Config(
        target=Target(image="fixture:test", port=8000),
        deployment=Deployment(
            termination_grace_period="70ms",
            drain=Drain(strategy=DrainStrategy.NONE),
        ),
    )
    report.facts["shutdown_started"] = True
    report.teardown_calibration = TeardownCalibration(
        (40.0, 50.0, 60.0, 50.0, 50.0)
    )
    result = _resolved(report)
    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "budget_below_teardown_floor",
    )
