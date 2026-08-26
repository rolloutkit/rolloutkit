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


def test_accept_then_reset_fails_for_in_app() -> None:
    result = _result(_report(DrainStrategy.IN_APP, reset=True))
    assert (result.status, result.branch) == (Status.FAIL, "accept_then_reset")


def test_none_always_warns_even_when_a_connection_resets() -> None:
    result = _result(_report(DrainStrategy.NONE, reset=True))
    assert (result.status, result.branch) == (Status.WARN, "none_uncovered")


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


def _attempts_from(
    report: RunReport, started: int, *, unanswered: int = 0, refused: int = 0
) -> RunReport:
    """Probe attempts every 50ms from `started`, which is what the probe does.

    The outcomes are what the peer did with them. TIMEOUT is a dropped SYN, which
    is a listening socket whose accept queue is full; REFUSED is an RST, which is
    no listening socket at all.
    """
    outcomes = [AcceptOutcome.TIMEOUT] * unanswered + [AcceptOutcome.REFUSED] * refused
    for index, outcome in enumerate(outcomes, start=1):
        started += 50_000_000
        report.accept_attempts.append(
            AcceptAttempt(
                started_ns=started,
                connected_ns=None,
                finished_ns=started + 1_000_000,
                outcome=outcome,
                error=f"attempt {index}",
            )
        )
    return report


def _after_last_accept(
    report: RunReport, *, unanswered: int = 0, refused: int = 0
) -> RunReport:
    """Everything the probe tried between the last accepted connection and T0."""
    started = max(attempt.started_ns for attempt in report.accept_attempts)
    return _attempts_from(report, started, unanswered=unanswered, refused=refused)


def _unmeasured(strategy: DrainStrategy, **counts) -> RunReport:
    report = _report(strategy, accept_ms=-2200)
    report.facts["shutdown_started"] = True
    return _after_last_accept(report, **counts)


def test_sp004_last_accept_inside_one_probe_interval_is_still_a_measurement() -> None:
    """The sign alone is not the signal.

    The probe samples every 50ms, so a listener that closed exactly at T0 leaves
    its last accept anywhere in the 50ms before it. Refusing that reading would
    throw away a genuine early close.
    """
    report = _report(DrainStrategy.IN_APP, accept_ms=-40)
    report.facts["shutdown_started"] = True
    result = _resolved(report)
    assert (result.status, result.branch) == (
        Status.FAIL,
        "in_app_listener_closed_early",
    )


def test_sp004_last_accept_further_back_than_the_interval_is_inconclusive() -> None:
    result = _resolved(_unmeasured(DrainStrategy.IN_APP, refused=6))
    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "accept_window_unmeasured",
    )
    assert "the probe stopped being accepted before T0" in result.summary
    assert "accept window not measured" in result.summary


def test_sp004_names_a_saturated_backlog_when_the_refusals_name_it() -> None:
    """Unanswered SYNs are a full accept queue; RSTs are a closed listener."""
    result = _resolved(_unmeasured(DrainStrategy.IN_APP, unanswered=5, refused=1))
    assert result.branch == "accept_window_unmeasured"
    assert result.summary.startswith("probe saturated the backlog")


def test_sp004_unmeasured_accept_window_keeps_the_raw_value() -> None:
    result = _resolved(_unmeasured(DrainStrategy.IN_APP, unanswered=5, refused=1))
    precondition = result.evidence["precondition"]
    assert precondition["accept_window_ms"] == -2200
    assert precondition["cause"] == "last_accept_before_t0"
    assert precondition["mechanism"] == "backlog_saturated"
    assert (
        precondition["unanswered_before_t0"],
        precondition["refused_before_t0"],
    ) == (5, 1)
    assert result.evidence["unresolved_candidate"] == {
        "status": "FAIL",
        "branch": "in_app_listener_closed_early",
    }


def test_sp004_no_sample_between_the_last_accept_and_t0_says_the_probe_was_busy() -> None:
    """The probe is serial: one slow attempt is one unsampled stretch of time.

    Nothing here was refused before the signal, so nothing here may be reported
    as the probe being refused before the signal.
    """
    report = _unmeasured(DrainStrategy.IN_APP)
    assert not [
        attempt
        for attempt in report.accept_attempts
        if attempt.connected_ns is None and attempt.started_ns < report.sigterm_ns
    ]
    result = _resolved(report)
    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "accept_window_unmeasured",
    )
    assert result.summary.startswith(
        "the probe was still waiting on its last accepted connection"
    )
    assert result.evidence["precondition"]["mechanism"] == "probe_blocked"


def test_sp004_refusals_after_t0_do_not_name_a_mechanism_before_it() -> None:
    """Post-mortem refusals describe the exit, not the interval that was missed.

    Counting them would let three RSTs from an already-dead process report the
    listener as gone before a signal it was still serving through.
    """
    report = _unmeasured(DrainStrategy.IN_APP)
    _attempts_from(report, report.sigterm_ns, refused=3)
    result = _resolved(report)
    assert result.evidence["precondition"]["mechanism"] == "probe_blocked"
    assert (
        result.evidence["precondition"]["refused_before_t0"],
        result.evidence["precondition"]["unanswered_before_t0"],
    ) == (0, 0)


def test_sp004_never_accepted_is_inconclusive_not_a_zero_window() -> None:
    report = _report(DrainStrategy.IN_APP)
    report.facts["shutdown_started"] = True
    report.accept_attempts = []
    result = _resolved(report)
    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "accept_window_unmeasured",
    )
    assert result.evidence["precondition"]["cause"] == "never_accepted"


def test_sp004_prestop_does_not_need_a_measured_accept_window() -> None:
    """The probe is stopped at T0 by design, so the last accept is always early."""
    result = _resolved(_unmeasured(DrainStrategy.PRESTOP, refused=6))
    assert (result.status, result.branch) == (Status.PASS, "prestop_not_applicable")


def test_sp004_none_does_not_need_a_measured_accept_window() -> None:
    result = _resolved(_unmeasured(DrainStrategy.NONE, refused=6))
    assert (result.status, result.branch) == (Status.WARN, "none_uncovered")


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
