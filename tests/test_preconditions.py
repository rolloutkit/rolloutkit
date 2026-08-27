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
from preflightkit.contracts.drain import DrainWindowContract
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
    assert results["SP005"].evidence["completion"] == {
        "completed": 1,
        "in_flight_at_sigterm": 1,
        "completion_rate": 1.0,
    }

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


def test_sp005_completion_rate_keeps_the_counts_visible() -> None:
    report = _finished_report()
    reset = _completed_request()
    reset.request_id = 2
    reset.outcome = Outcome.RESET_BEFORE_RESPONSE
    reset.status = None
    report.requests.append(reset)

    result = _by_id(report)["SP005"]

    assert result.status is Status.FAIL
    assert result.evidence["completion"] == {
        "completed": 1,
        "in_flight_at_sigterm": 2,
        "completion_rate": 0.5,
    }


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


def test_explicitly_disabled_sp005_is_skip_before_measurement_preconditions() -> None:
    report = RunReport(
        config=Config(
            target=Target(image="fixture:latest", port=8000),
            contracts=Contracts(inflight=None),
        )
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

    assert (result.status, result.branch) == (Status.SKIP, "disabled")
    assert "explicitly disabled" in result.summary


def test_readiness_fallback_below_jitter_resolution_is_inconclusive() -> None:
    report = _finished_report()
    report.config = Config(target=Target(image="fixture:latest", port=8000))
    report.inflight_target = "readiness_fallback"
    report.inflight_path = "/ready"
    report.inflight_fallback_p50_ms = 8.0
    report.inflight_fallback_jitter_ms = 1.8
    report.inflight_fallback_ratio = 8.0 / 1.8
    report.baseline = None
    report.requests = []

    result = _by_id(report)["SP005"]

    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "readiness_fallback_below_resolution",
    )
    assert "8.0ms" in result.summary
    assert "1.8ms" in result.summary
    assert "--inflight-path" in result.summary
    assert result.evidence["precondition"]["inflight_target"] == "readiness_fallback"


def test_readiness_fallback_below_absolute_floor_is_inconclusive() -> None:
    """A clean ratio over a window that is short in absolute terms.

    2.0ms against 0.15ms of jitter is 13.3x, comfortably over the required 10x,
    and the current rule would have measured it. It is here because that pair of
    numbers is what an idle macOS laptop actually produced for a 1ms readiness
    delay, and a native Linux runner refused the identical image because its own
    jitter floor is 3.4x higher. The ratio is not wrong about what it measures;
    it just cannot see that the window is too short to survive being carried to
    another host.
    """
    report = _finished_report()
    report.config = Config(target=Target(image="fixture:latest", port=8000))
    report.inflight_target = "readiness_fallback"
    report.inflight_path = "/ready"
    report.inflight_fallback_p50_ms = 2.0
    report.inflight_fallback_jitter_ms = 0.15
    report.inflight_fallback_ratio = 2.0 / 0.15
    report.baseline = None
    report.requests = []

    result = _by_id(report)["SP005"]

    assert (result.status, result.branch) == (
        Status.INCONCLUSIVE,
        "readiness_fallback_below_resolution",
    )
    precondition = result.evidence["precondition"]
    assert precondition["cause"] == "below_window"
    # The ratio cleared its gate. If this ever fails the test has stopped
    # covering the case it was written for and is duplicating the one above.
    assert precondition["ratio"] >= precondition["minimum_ratio"]
    assert "3ms" in result.summary
    assert "--inflight-path" in result.summary


def test_readiness_fallback_names_the_ratio_when_the_ratio_refused() -> None:
    """Both clauses fail, and the reported cause is the ratio.

    Ordering, asserted rather than assumed. A 0.4ms window against 0.2ms of
    jitter fails the floor and the ratio both, and the floor must not take
    credit for a decision the ratio had already made — otherwise every reading
    of these verdicts undercounts how often the ratio is the binding constraint.
    """
    report = _finished_report()
    report.config = Config(target=Target(image="fixture:latest", port=8000))
    report.inflight_target = "readiness_fallback"
    report.inflight_path = "/ready"
    report.inflight_fallback_p50_ms = 0.4
    report.inflight_fallback_jitter_ms = 0.2
    report.inflight_fallback_ratio = 2.0
    report.baseline = None
    report.requests = []

    result = _by_id(report)["SP005"]

    assert result.evidence["precondition"]["cause"] == "below_ratio"


def test_readiness_fallback_resolves_when_both_clauses_pass() -> None:
    """The guard is a floor, not a second decision.

    5ms at 0.15ms of jitter clears both, and SP005 goes on to measure. Without
    this the suite would only ever prove that the new clause can refuse.
    """
    report = _finished_report()
    report.inflight_target = "readiness_fallback"
    report.inflight_path = "/ready"
    report.inflight_fallback_p50_ms = 5.0
    report.inflight_fallback_jitter_ms = 0.15
    report.inflight_fallback_ratio = 5.0 / 0.15

    result = _by_id(report)["SP005"]

    assert result.branch != "readiness_fallback_below_resolution"
    assert result.status is not Status.INCONCLUSIVE


_CLOSED_EARLY = "The window closed early"
_EXPERIMENT_CLAIM = "The experiment and traffic measurement completed"


def test_refused_fallback_describes_only_the_refusal() -> None:
    """A refusal must not arrive with a second, competing cause beside it.

    The fallback precondition refuses before the long-request phase runs, so no
    request is ever issued. The candidate verdict computed over that skipped
    phase is still kept in evidence, and it used to bring two notes with it:
    "0 of 0 requests finished before the signal. The window closed early",
    which counts an experiment that never happened, and a claim that the
    experiment had completed. Either one reads as the reason SP005 came back
    INCONCLUSIVE, and neither is.
    """
    report = _finished_report()
    report.config = Config(target=Target(image="fixture:latest", port=8000))
    report.inflight_target = "readiness_fallback"
    report.inflight_path = "/ready"
    report.inflight_fallback_p50_ms = 0.6
    report.inflight_fallback_jitter_ms = 0.6
    report.inflight_fallback_ratio = 1.0
    report.baseline = None
    report.requests = []

    result = _by_id(report)["SP005"]

    assert result.branch == "readiness_fallback_below_resolution"
    assert not any(_CLOSED_EARLY in note for note in result.notes)
    assert not any(_EXPERIMENT_CLAIM in note for note in result.notes)
    # The candidate is still in evidence, and one note still says why it is
    # there — it just no longer claims anything about how far the run got.
    assert result.evidence["unresolved_candidate"] == {
        "status": "ERROR",
        "branch": "nothing_in_flight",
    }
    assert any("kept in evidence, but not published" in n for n in result.notes)


def test_a_genuinely_empty_window_keeps_the_closed_early_note() -> None:
    """The other direction: requests were issued, and all of them finished.

    This is the case the note was written for — a real experiment whose window
    closed before the signal — and suppressing it along with the "0 of 0" one
    would cost the reader the only advice that names sigterm_after.
    """
    report = _finished_report()
    early = _completed_request()
    early.started_ns = 3 * SECOND
    early.connected_ns = 3 * SECOND
    early.request_sent_ns = 3 * SECOND
    early.finished_ns = 4 * SECOND
    report.requests = [early]

    result = _by_id(report)["SP005"]

    assert (result.status, result.branch) == (Status.ERROR, "nothing_in_flight")
    assert any("1 of 1 requests finished" in note for note in result.notes)
    assert any(_CLOSED_EARLY in note for note in result.notes)


def test_none_drain_warns_even_when_shutdown_never_started() -> None:
    report = RunReport(
        config=Config(target=Target(image="fixture:latest", port=8000))
    )

    result = evaluate_contracts(report, (DrainWindowContract(),))[0]

    assert (result.status, result.branch) == (Status.WARN, "none_uncovered")


def test_connection_close_is_not_applicable_without_steady_state_keepalive() -> None:
    report = _finished_report()
    report.baseline = _baseline(keep_alive=False)
    report.requests = [_completed_request(connection_close=True)]

    evidence = _by_id(report)["SP005"].evidence["keepalive_closed_cleanly"]

    assert evidence["status"] == "not_applicable"
    assert evidence["announced_connection_close"] is None
    assert "steady-state" in evidence["reason"]


def test_required_inconclusive_blocks_gating_unless_explicitly_allowed(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PREFLIGHTKIT_COMMIT", "0123456789abcdef")
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
    assert document["preflightkit_commit"] == "0123456789abcdef"
    assert document["result"] != "INCONCLUSIVE"
    sp005_document = next(
        contract for contract in document["contracts"] if contract["id"] == "SP005"
    )
    assert "candidate_status" not in sp005_document["actual"]
    assert sp005_document["evidence"]["unresolved_candidate"]["status"] == "PASS"
    assert document["runs"][0]["container_start_overhead_ms"] == 752.0
    assert document["runs"][0]["startup_resolution_ms"] == 852.0
    assert [r.id for r in _blocking_results(session, FailOn.ERROR, False)] == [
        "SP005"
    ]
    assert _blocking_results(session, FailOn.ERROR, True) == []
    assert _blocking_results(session, FailOn.NONE, False) == []


def test_required_skip_blocks_gating_unless_explicitly_allowed() -> None:
    report = RunReport(
        config=Config(
            target=Target(image="fixture:latest", port=8000),
            contracts=Contracts(inflight=None),
        )
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
