"""Verdict logic, without a container.

The fixtures prove the contracts work against real images; these prove the
branch chosen for a given report is the right one, including the combinations
that are awkward to produce on demand. Both matter — the defect that motivated
this file was a FAIL branch guarded on a condition no real run could satisfy,
and only a synthetic report can show that directly.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from preflightkit.config.models import Config, Deployment, Target
from preflightkit.contracts.base import Status
from preflightkit.contracts.deadline import DeadlineContract
from preflightkit.contracts.signals import SignalContract
from preflightkit.contracts.startup import StartupContract
from preflightkit.engine.context import RunReport
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.reporters import terminal
from preflightkit.runtime.base import DaemonEvent, Pid1Facts

SECOND = 1_000_000_000


def _report(
    *,
    budget: str = "30s",
    pre_stop_ms: int = 0,
    shutdown_ms: float | None = 500,
    exit_code: int | None = 0,
    sigkill_sent: bool = False,
    stop_signal: str | None = None,
    pid1: Pid1Facts | None = None,
    readiness_changed: bool = False,
) -> RunReport:
    deployment = Deployment(termination_grace_period=budget)
    config = Config(target=Target(image="example:latest", port=8000), deployment=deployment)
    report = RunReport(config=config)
    report.sigterm_ns = 10 * SECOND
    if shutdown_ms is not None:
        report.exit_ns = report.sigterm_ns + int(shutdown_ms * 1_000_000)
    report.exit_code = exit_code
    report.sigkill_sent = sigkill_sent
    if readiness_changed:
        report.readiness_drop_ns = report.sigterm_ns + 10_000_000
    if stop_signal:
        report.image_config = {"StopSignal": stop_signal}
    report.pid1 = pid1
    return report


#: SIGTERM is signal 15, so bit 14. These are the two bitmasks measured on real
#: images: CPython installs nothing for it, the Go runtime installs everything.
_NO_HANDLER = Pid1Facts(comm="python", sig_caught=0x2, sig_ignored=0, sig_blocked=0)
_HANDLER = Pid1Facts(comm="uvicorn", sig_caught=0x4002, sig_ignored=0, sig_blocked=0)
_IGNORED = Pid1Facts(comm="app", sig_caught=0x2, sig_ignored=0x4000, sig_blocked=0)


def test_sp001_reports_container_start_overhead_separately() -> None:
    report = _report()
    report.container_started_ns = SECOND
    report.container_start_overhead_ms = 752.0
    report.tcp_open_ns = 4 * SECOND
    report.readiness_ok_ns = 4_100_000_000
    report.readiness_status = 200

    result = StartupContract().evaluate(report)

    assert result.actual["container_start_overhead_ms"] == 752.0
    assert result.actual["startup_resolution_ms"] == 852.0
    assert result.actual["tcp_open_ms"] == 3000.0


def test_sp001_ignores_budget_overrun_inside_startup_resolution() -> None:
    report = _report()
    report.config = report.config.model_copy(
        update={
            "contracts": report.config.contracts.model_copy(
                update={
                    "startup": report.config.contracts.startup.model_copy(
                        update={"budget": 1000}
                    )
                }
            )
        }
    )
    report.container_started_ns = SECOND
    report.container_start_overhead_ms = 752.0
    report.readiness_ok_ns = 2_500_000_000

    result = StartupContract().evaluate(report)

    assert (result.status, result.branch) == (Status.PASS, "within_resolution")
    assert result.actual["startup_resolution_ms"] == 852.0


def test_sp001_warns_when_overrun_exceeds_startup_resolution() -> None:
    report = _report()
    report.config = report.config.model_copy(
        update={
            "contracts": report.config.contracts.model_copy(
                update={
                    "startup": report.config.contracts.startup.model_copy(
                        update={"budget": 1000}
                    )
                }
            )
        }
    )
    report.container_started_ns = SECOND
    report.container_start_overhead_ms = 500.0
    report.readiness_ok_ns = 2_700_000_000

    result = StartupContract().evaluate(report)

    assert (result.status, result.branch) == (Status.WARN, "over_budget")


def test_terminal_exposes_startup_resolution() -> None:
    report = _report()
    report.container_started_ns = SECOND
    report.container_start_overhead_ms = 752.0
    report.tcp_open_ns = 4 * SECOND
    report.readiness_ok_ns = 4_100_000_000
    report.readiness_status = 200
    outcome = RunOutcome(report=report, results=[StartupContract().evaluate(report)])
    session = Session(run_id="pfk_test", image="example:latest", runs=[outcome])
    console = Console(record=True, width=140)

    terminal.render(session, "test", console)
    rendered = console.export_text()

    assert "startup resolution" in rendered
    assert "852ms" in rendered


# --- SP006 -----------------------------------------------------------------


def test_sigkill_is_a_failure_even_when_the_clock_looks_fine() -> None:
    """The defect this file exists for.

    A killed process still exits, so `shutdown_duration_ms` is still recorded and
    still lands inside the budget. Judging on the clock alone reported WARN over
    a process that never shut itself down.
    """
    report = _report(budget="250ms", shutdown_ms=200, exit_code=137, sigkill_sent=True)
    result = DeadlineContract().evaluate(report)
    assert result.status is Status.FAIL
    assert result.branch == "past_deadline"


def test_overrunning_the_budget_fails_without_a_sigkill() -> None:
    report = _report(budget="1s", shutdown_ms=1500, exit_code=0)
    result = DeadlineContract().evaluate(report)
    assert result.status is Status.FAIL
    assert result.branch == "past_deadline"
    assert result.actual["margin_ms"] < 0
    # The old WARN text read "only -107ms under the budget", which is not English
    # and not a finding. Whatever the wording becomes, it may not claim the
    # process finished inside the budget.
    assert "under the" not in result.summary


def test_a_thin_margin_warns_and_reports_a_positive_margin() -> None:
    report = _report(budget="6s", shutdown_ms=5000, exit_code=0)
    result = DeadlineContract().evaluate(report)
    assert result.status is Status.WARN
    assert result.branch == "thin_margin"
    assert result.actual["margin_ms"] > 0


def test_a_comfortable_margin_passes() -> None:
    report = _report(budget="30s", shutdown_ms=500, exit_code=0)
    result = DeadlineContract().evaluate(report)
    assert result.status is Status.PASS
    assert result.branch == "within_budget"


def test_a_sent_but_ineffective_sigkill_does_not_fail_the_deadline() -> None:
    """Measured on a real image: SIGKILL at +275ms, exit code 0 three runs running.

    Our timer fired; the process had already gone. Failing on that would be the
    tool reporting its own behaviour as the target's.
    """
    report = _report(budget="30s", shutdown_ms=400, exit_code=0, sigkill_sent=True)
    result = DeadlineContract().evaluate(report)
    assert result.status is Status.PASS
    assert result.actual["sigkill_sent"] is True
    assert result.actual["sigkill_effective"] is False


# --- SP003 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "stop_signal", "status", "branch"),
    [
        (143, None, Status.PASS, "shutdown_observed"),
        (2, None, Status.PASS, "shutdown_observed"),
        (0, "SIGQUIT", Status.PASS, "shutdown_observed"),
        (0, None, Status.PASS, "shutdown_observed"),
    ],
)
def test_signal_branches(
    exit_code: int | None,
    stop_signal: str | None,
    status: Status,
    branch: str,
) -> None:
    report = _report(
        exit_code=exit_code,
        stop_signal=stop_signal,
        shutdown_ms=None if exit_code is None else 500,
    )
    result = SignalContract().evaluate(report)
    assert (result.status, result.branch) == (status, branch)


def test_a_sent_but_ineffective_sigkill_passes_and_says_why() -> None:
    """`sigkill_sent` records what we did; the verdict follows what happened."""
    report = _report(exit_code=0, sigkill_sent=True)
    result = SignalContract().evaluate(report)
    assert result.status is Status.PASS
    assert result.evidence["sigkill_sent"] is True
    assert any("already on its way out" in note for note in result.notes)


@pytest.mark.parametrize("exit_code", [0, 2, 143])
def test_exit_code_is_evidence_not_a_signal_verdict(exit_code: int) -> None:
    """The same observed shutdown has the same verdict behind PID 1 or init."""
    result = SignalContract().evaluate(_report(exit_code=exit_code, shutdown_ms=82))
    assert (result.status, result.branch) == (Status.PASS, "shutdown_observed")
    assert result.evidence["exit_code"] == exit_code
    assert result.facts["shutdown_started"] is True


def test_sigkill_effective_fails_after_an_observed_reaction() -> None:
    result = SignalContract().evaluate(
        _report(exit_code=137, sigkill_sent=True, readiness_changed=True)
    )
    assert (result.status, result.branch) == (Status.FAIL, "killed")


def test_signal_contract_leaves_deadline_judgment_to_sp006() -> None:
    result = SignalContract().evaluate(
        _report(budget="1s", exit_code=0, shutdown_ms=1500)
    )
    assert (result.status, result.branch) == (Status.PASS, "shutdown_observed")


def test_a_discarded_signal_is_told_apart_from_a_missed_deadline() -> None:
    """Both end in SIGKILL and exit 137; the fix for each is different.

    Without /proc/1/status the only honest verdict is "SIGKILL ended it". With
    it, the run can say the application never received the signal at all — and
    that no grace period would have changed the outcome.
    """
    discarded = SignalContract().evaluate(_report(exit_code=137, pid1=_NO_HANDLER))
    assert (discarded.status, discarded.branch) == (Status.FAIL, "signal_discarded")
    assert "python" in discarded.summary
    assert any("init of a PID namespace" in note for note in discarded.notes)

    ignored_deadline = SignalContract().evaluate(
        _report(exit_code=137, pid1=_HANDLER, readiness_changed=True)
    )
    assert (ignored_deadline.status, ignored_deadline.branch) == (Status.FAIL, "killed")


def test_an_explicitly_ignored_signal_says_so() -> None:
    result = SignalContract().evaluate(_report(exit_code=137, pid1=_IGNORED))
    assert result.branch == "signal_discarded"
    assert any("SIG_IGN" in note for note in result.notes)


def test_an_unmeasured_disposition_falls_back_rather_than_guessing() -> None:
    """No probe image, no diagnosis - but still a verdict, and still a route to one."""
    result = SignalContract().evaluate(_report(exit_code=137, pid1=None))
    assert (result.status, result.branch) == (Status.FAIL, "shutdown_not_started")
    assert result.actual["runtime_handler_installed"] is None
    assert any("docker pull busybox" in note for note in result.notes)


def test_the_duration_prefers_the_daemons_own_clock() -> None:
    """Two timestamps from one clock beat two timestamps a round trip apart."""
    report = _report(exit_code=0, shutdown_ms=91)
    report.daemon_events = [
        DaemonEvent("kill", daemon_ns=1_000_000_000, observed_ns=0),
        DaemonEvent("die", daemon_ns=1_086_000_000, observed_ns=0),
    ]
    assert report.shutdown_duration_ms == pytest.approx(86)
    assert report.observed_shutdown_duration_ms == pytest.approx(91)
    assert report.shutdown_duration_source == "daemon_events"
    assert report.observation_lag_ms == pytest.approx(5)


def test_the_duration_starts_at_sigterm_not_at_the_sigkill_that_followed() -> None:
    """A run that blows its budget produces two `kill` frames. The first is T0."""
    report = _report(exit_code=137, shutdown_ms=4200)
    report.daemon_events = [
        DaemonEvent("kill", daemon_ns=1_000_000_000, observed_ns=0),
        DaemonEvent("kill", daemon_ns=5_000_000_000, observed_ns=0),
        DaemonEvent("die", daemon_ns=5_100_000_000, observed_ns=0),
    ]
    assert report.shutdown_duration_ms == pytest.approx(4100)


def test_without_daemon_frames_the_duration_falls_back_and_says_so() -> None:
    report = _report(exit_code=0, shutdown_ms=91)
    assert report.shutdown_duration_source == "observed"
    result = SignalContract().evaluate(report)
    assert any("no `kill`/`die` frames" in note for note in result.notes)


def test_a_shutdown_faster_than_the_floor_is_not_reported_as_the_apps_doing() -> None:
    """The Go fixture, and the reason the floor is calibrated at all.

    Its measured shutdown was 73ms against a floor of 79ms — the whole number was
    the daemon tearing down a published port. Saying "exited in 73ms" without
    saying that invites the reader to credit the application with a speed nothing
    here measured.
    """
    report = _report(shutdown_ms=73, exit_code=2)
    report.teardown_floor_ms = 79.0
    notes = SignalContract().evaluate(report).notes
    assert any("at or below this host's floor" in note for note in notes)
    assert not any("of the 73ms is the daemon" in note for note in notes)


def test_the_floor_stays_quiet_when_a_real_drain_dominates_the_duration() -> None:
    """80ms of daemon inside a three-second drain is noise, and saying so is
    noise too. The note is worth printing only when it changes how the number
    should be read."""
    report = _report(shutdown_ms=3130, exit_code=0)
    report.teardown_floor_ms = 79.0
    notes = SignalContract().evaluate(report).notes
    assert not any("floor" in note for note in notes)
