"""SP004 verdict table, one explicit test per branch."""

from __future__ import annotations

from dataclasses import replace

import pytest
from rich.console import Console

from rolloutkit.config.models import Config, Deployment, Drain, DrainStrategy, Target
from rolloutkit.contracts.base import ContractResult, Status
from rolloutkit.contracts.drain import (
    DrainWindowContract,
    accept_window_unmeasured_reason,
)
from rolloutkit.engine.context import RunReport
from rolloutkit.engine.preconditions import evaluate_contracts
from rolloutkit.evidence.model import RunOutcome, Session
from rolloutkit.reporters import terminal
from rolloutkit.runtime.base import DaemonEvent, TeardownCalibration
from rolloutkit.traffic.accept_probe import AcceptAttempt, AcceptOutcome

SECOND = 1_000_000_000


def _report(
    strategy: DrainStrategy,
    *,
    window_ms: int = 1200,
    accept_ms: float = 1600,
    readiness: str | None = "status_change",
    shutdown_ms: float = 500,
    reset: bool = False,
    started_ms: float | None = None,
) -> RunReport:
    """`accept_ms` is when the handshake completed; `started_ms` when it was asked
    for. They are separate parameters because SP004 keys the reset branch on the
    second one, and a builder that derived it from the first could not write a run
    where the two fall on opposite sides of the window."""
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
    started = (
        connected - 1_000_000
        if started_ms is None
        else report.sigterm_ns + int(started_ms * 1_000_000)
    )
    report.accept_attempts = [
        AcceptAttempt(
            started_ns=started,
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
    """Asked for inside the window, handshaken after it: still the failure.

    The two offsets are deliberately on opposite sides of the 1200ms boundary.
    The caller asked at +900ms, while the application had 300ms of its declared
    window left to run, and was answered with a reset. That the handshake did
    not complete until +1600ms is the target's own accept latency; charging the
    caller for it would move a connection across the boundary because the
    target was busy, which is the thing being measured.
    """
    report = _report(DrainStrategy.IN_APP, reset=True, started_ms=900)
    result = _result(report)
    assert (result.status, result.branch) == (Status.FAIL, "accept_then_reset")
    assert result.actual["accept_then_reset_in_window"] == 1
    assert result.actual["accept_then_reset_after_window"] == 0
    assert "+900ms" in result.summary


def test_a_reset_asked_for_after_the_window_is_evidence_not_a_verdict() -> None:
    """The population `fixtures/backlog-reset/` exists for.

    Closing a listening socket resets whatever the kernel has already
    handshaken into its accept queue. A server that served its whole window and
    then closed produces this on every run under load, and it is not a defect:
    the connection was opened after the application had done what it declared.
    """
    result = _result(_report(DrainStrategy.IN_APP, reset=True, started_ms=1500))
    assert (result.status, result.branch) == (Status.PASS, "in_app_covered")
    assert result.actual["accept_then_reset"] == 1
    assert result.actual["accept_then_reset_in_window"] == 0
    assert result.actual["accept_then_reset_after_window"] == 1
    assert any("after the 1200ms window had already closed" in n for n in result.notes)


def test_a_reset_asked_for_on_the_last_millisecond_is_inside_the_window() -> None:
    """The boundary is closed at the top: a window of 1200ms includes +1200ms."""
    inside = _result(_report(DrainStrategy.IN_APP, reset=True, started_ms=1200))
    assert (inside.status, inside.branch) == (Status.FAIL, "accept_then_reset")
    outside = _result(_report(DrainStrategy.IN_APP, reset=True, started_ms=1200.001))
    assert outside.branch == "in_app_covered"


def test_the_reset_evidence_carries_both_offsets_and_the_window() -> None:
    """The verdict has to be checkable from the report on its own.

    Before this, the window the resets were classified against appeared only
    inside the `expected` sentence, and only `connected_offset_ms` was
    published — so a reader had a list of events, a prose boundary, and the
    wrong one of the two timestamps needed to reproduce the branch.
    """
    result = _result(_report(DrainStrategy.IN_APP, reset=True, started_ms=900))
    assert result.evidence["in_app_window_ms"] == 1200
    (event,) = result.evidence["accept_then_reset"]
    assert event["started_offset_ms"] == pytest.approx(900)
    assert event["connected_offset_ms"] == pytest.approx(1600)
    assert event["started_offset_ms"] < result.evidence["in_app_window_ms"]


def test_the_window_is_not_published_for_strategies_that_do_not_declare_one() -> None:
    assert _result(_report(DrainStrategy.NONE)).evidence["in_app_window_ms"] is None
    assert _result(_report(DrainStrategy.PRESTOP)).evidence["in_app_window_ms"] is None


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


def _render(report: RunReport, result: ContractResult) -> str:
    outcome = RunOutcome(report=report, results=[result])
    session = Session(run_id="rk_test", image="fixture:test", runs=[outcome])
    console = Console(record=True, width=140)
    terminal.render(session, "test", console)
    return console.export_text()


def test_terminal_prints_accept_window_with_its_resolution() -> None:
    report = _report(DrainStrategy.IN_APP)
    assert "+1600ms (±50ms)" in _render(report, _result(report))


def test_terminal_prints_notes_under_a_passing_contract() -> None:
    """The finding this branch exists to surface has to reach the reader.

    SP004 passes when every reset was asked for after its window had closed, and
    the note is the only place the run says those connections were reset at all.
    A reporter that printed notes only under a red status would withhold it
    exactly where the verdict says nothing is wrong.
    """
    report = _report(DrainStrategy.IN_APP, reset=True, started_ms=1500)
    result = _result(report)
    assert result.status is Status.PASS

    rendered = _render(report, result)

    assert "after the 1200ms window had already closed" in rendered


def test_a_passing_contract_without_notes_still_prints_one_line() -> None:
    """The other half of the change: quiet rows stay quiet.

    Most PASS rows carry no notes, and printing notes unconditionally must not
    add anything to them - the contract line is still the whole of their output.
    """
    report = _report(DrainStrategy.IN_APP)
    result = replace(_result(report), notes=[])

    rendered = _render(report, result)

    assert "SP004" in rendered
    assert "        - " not in rendered


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
    assert "the listener had already stopped accepting" in result.summary
    assert result.summary.startswith("accept window not measured")


def test_sp004_names_a_saturated_backlog_when_the_refusals_name_it() -> None:
    """Unanswered SYNs are a full accept queue; RSTs are a closed listener."""
    result = _resolved(_unmeasured(DrainStrategy.IN_APP, unanswered=5, refused=1))
    assert result.branch == "accept_window_unmeasured"
    assert "the target's accept queue was dropping the probe's SYNs" in result.summary


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
    assert "the probe was still busy on an earlier connection" in result.summary
    assert result.evidence["precondition"]["mechanism"] == "probe_blocked"


#: A contract summary is a line in a table the reader is scanning. The old
#: unmeasured text ran to 318 characters — four wrapped lines under the row —
#: and a wall that size is a thing readers skip, which costs more than the
#: detail it carried.
MAX_SUMMARY_CHARS = 170


def test_the_unmeasured_summary_stays_one_scannable_sentence() -> None:
    """What the summary owes the reader, and what it does not.

    It owes the mechanism and how much time went unsampled. It does not owe the
    probe interval, the rule that classifies against it, or the attempt list:
    those are in `explain SP004` and in the JSON, which is where they can be
    read at length instead of skimmed past.
    """
    summaries = {
        "probe_blocked": _resolved(_unmeasured(DrainStrategy.IN_APP)).summary,
        "backlog_saturated": _resolved(
            _unmeasured(DrainStrategy.IN_APP, unanswered=5, refused=1)
        ).summary,
        "listener_gone": _resolved(
            _unmeasured(DrainStrategy.IN_APP, refused=6)
        ).summary,
        "never_accepted": accept_window_unmeasured_reason(
            _unmeasured(DrainStrategy.IN_APP), "never_accepted"
        ),
    }
    for mechanism, summary in summaries.items():
        assert len(summary) <= MAX_SUMMARY_CHARS, f"{mechanism}: {len(summary)}"
        assert ". " not in summary, f"{mechanism} grew a second sentence"
        assert summary.startswith("accept window not measured"), mechanism
    assert len(set(summaries.values())) == len(summaries), (
        "each mechanism must still be distinguishable from the summary alone"
    )


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


#: One run per adjacent pair in `IN_APP_PRECEDENCE`, built so that both clauses
#: of the pair answer it. That is the only way an order is observable at all: a
#: precedence over clauses that never co-occur is decoration, and a precedence
#: over clauses that do is the difference between reporting a lost connection
#: and reporting a suggestion. Swapping any two neighbours in the contract's
#: tuple turns one of these red.
#:
#: `in_app_covered` answers unconditionally — it is the tail the loop is
#: guaranteed to reach — so the pair above it is pinned like every other.
_PRECEDENCE_PAIRS = {
    ("accept_then_reset", "in_app_listener_closed_early"): dict(
        accept_ms=700, started_ms=600, reset=True
    ),
    ("in_app_listener_closed_early", "in_app_readiness_not_signaled"): dict(
        accept_ms=700, readiness=None
    ),
    ("in_app_readiness_not_signaled", "in_app_thin_margin"): dict(
        accept_ms=1300, readiness=None
    ),
    ("in_app_thin_margin", "in_app_covered"): dict(accept_ms=1300),
}


@pytest.mark.parametrize(("pair", "kwargs"), sorted(_PRECEDENCE_PAIRS.items()))
def test_the_higher_in_app_clause_answers_when_both_hold(
    pair: tuple[str, str], kwargs: dict
) -> None:
    winner, loser = pair
    result = _result(_report(DrainStrategy.IN_APP, **kwargs))
    assert result.branch == winner, (
        f"both {winner} and {loser} hold on this run; SP004 answered "
        f"{result.branch}, which reverses the declared precedence"
    )
    assert result.status is DrainWindowContract.BRANCHES[winner]


def test_every_step_of_the_declared_precedence_is_pinned() -> None:
    """A tuple nobody tests is a comment that happens to be executable."""
    order = DrainWindowContract.IN_APP_PRECEDENCE
    adjacent = set(zip(order, order[1:], strict=False))
    assert adjacent == set(_PRECEDENCE_PAIRS), (
        "IN_APP_PRECEDENCE changed without a run that shows the new order: "
        f"unpinned {sorted(adjacent - set(_PRECEDENCE_PAIRS))}, "
        f"stale {sorted(set(_PRECEDENCE_PAIRS) - adjacent)}"
    )


def test_a_reset_outranks_an_unsignalled_readiness() -> None:
    """The run this order was written for, and the one it is easiest to get wrong.

    A target that never signals readiness while resetting a connection accepted
    after T0 satisfies two clauses at once, and they disagree by two statuses.
    `accept_then_reset` is a client that already believed it had a connection;
    `in_app_readiness_not_signaled` is a drain that worked and could not be
    watched. Reporting the advisory would call a lost connection a suggestion.
    """
    result = _result(
        _report(DrainStrategy.IN_APP, reset=True, started_ms=900, readiness=None)
    )
    assert (result.status, result.branch) == (Status.FAIL, "accept_then_reset")
    assert result.actual["accept_then_reset"] == 1
    assert result.actual["readiness_drop_mode"] == "never"


def test_the_precedence_names_every_in_app_verdict_exactly_once() -> None:
    """The loop can only answer with a branch the tuple names."""
    order = DrainWindowContract.IN_APP_PRECEDENCE
    assert len(set(order)) == len(order), "a branch is listed twice"
    in_app = {
        branch
        for branch, status in DrainWindowContract.BRANCHES.items()
        if status is not Status.INCONCLUSIVE
        and branch not in ("prestop_not_applicable", "none_uncovered")
    }
    assert set(order) == in_app, (
        "an in_app branch outside the precedence is unreachable: "
        f"{sorted(in_app - set(order))}"
    )


def test_the_precedence_never_puts_an_advisory_above_a_failure() -> None:
    """Worst first. The order may be argued within a status, never across one."""
    severity = {Status.FAIL: 0, Status.WARN: 1, Status.PASS: 2}
    ranks = [
        severity[DrainWindowContract.BRANCHES[branch]]
        for branch in DrainWindowContract.IN_APP_PRECEDENCE
    ]
    assert ranks == sorted(ranks), (
        "IN_APP_PRECEDENCE asks a lighter status before a heavier one: "
        + ", ".join(
            f"{branch} ({DrainWindowContract.BRANCHES[branch]})"
            for branch in DrainWindowContract.IN_APP_PRECEDENCE
        )
    )
