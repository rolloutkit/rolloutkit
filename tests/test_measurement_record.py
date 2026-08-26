"""Every run has to leave behind enough to choose a resolution threshold from.

Two constants in this tool are floors: `MIN_JITTER_RATIO`, which decides whether
a readiness window is distinguishable from measurement noise, and the teardown
stddev multiplier, which decides whether a shutdown budget is distinguishable
from the daemon's own overhead. Both were picked on one macOS laptop. Neither
can be defended from one machine, because both describe the machine.

The way out is not a separate measurement campaign — it is that every run
already measures exactly these numbers and then throws most of them away. These
tests hold the report to keeping them: per run, next to the phase durations, so
that a Linux server, a macOS laptop and a CI runner produce comparable rows
without anyone having to instrument anything first.

They assert structure and provenance, never magnitude. A test that asserted "the
ratio is above 10 here" would be one more thing riding a boundary on whichever
machine happened to run it, which is the failure this record exists to end.
"""

from __future__ import annotations

from preflightkit.config.models import Config, Target
from preflightkit.contracts.inflight import (
    MIN_JITTER_RATIO,
    MIN_READINESS_WINDOW_MS,
)
from preflightkit.engine.context import RunReport
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.probes.http import ProbeResult
from preflightkit.reporters import json_out
from preflightkit.runtime.base import TeardownCalibration
from preflightkit.traffic.baseline import ReadinessBaseline


def _report(
    *,
    jitter_ns: tuple[int, ...] = (1_100_000, 1_200_000, 1_300_000),
    readiness_ns: tuple[int, ...] = (2_400_000, 2_600_000, 3_000_000),
    load: tuple[float, float, float] | None = (1.0, 1.0, 1.0),
    inflight_target: str = "readiness_fallback",
) -> RunReport:
    report = RunReport(config=Config(target=Target(image="example:latest", port=8000)))
    report.host_os = "Darwin 25.5.0"
    report.cpu_count = 10
    report.load_average = load
    report.docker_server = {"version": "28.1.1", "os": "linux", "arch": "aarch64"}
    report.ping_latencies_ns = list(jitter_ns)
    report.probe_location = "sidecar"
    report.inflight_target = inflight_target
    report.readiness_baseline = ReadinessBaseline(
        samples=[
            ProbeResult(
                ok=True,
                status=200,
                latency_ns=value,
                headers={},
                body_head="",
                body_head_bytes=0,
            )
            for value in readiness_ns
        ]
    )
    return report


def _document(*reports: RunReport) -> dict:
    session = Session(
        run_id="pfk_test",
        image="example:latest",
        runs=[RunOutcome(report=report, results=[]) for report in reports],
    )
    return json_out.build(session, "test")


def test_one_run_carries_everything_a_threshold_would_be_chosen_from() -> None:
    """The four quantities, reachable from a single run without a second tool."""
    report = _report()
    report.teardown_calibration = TeardownCalibration(
        samples_ms=(210.0, 215.0, 221.0, 218.0, 214.0)
    )
    run = _document(report)["runs"][0]

    calibration = run["resolution_calibration"]
    assert calibration["host_id"]
    assert calibration["measurement_jitter_ms"] is not None
    assert calibration["readiness_p50_ms"] is not None
    teardown = run["teardown_calibration"]
    assert teardown["samples_ms"] and teardown["floor_ms"] and teardown["stddev_ms"] >= 0


def test_the_record_is_written_per_run_not_once_per_session() -> None:
    """Repeats exist to show spread; one summary of them shows none.

    `--repeat` is how a user asks the same machine the same question several
    times. If the record were built from the last run alone, the run that
    differed — the one under load, the one that explains the outlier — would be
    the one dropped.
    """
    quiet = _report(load=(0.4, 0.5, 0.6), jitter_ns=(900_000, 950_000, 1_000_000))
    busy = _report(load=(9.1, 8.4, 7.2), jitter_ns=(4_000_000, 4_500_000, 5_000_000))

    runs = _document(quiet, busy)["runs"]

    assert len(runs) == 2
    first, second = (run["resolution_calibration"] for run in runs)
    assert first["load_average"] == [0.4, 0.5, 0.6]
    assert second["load_average"] == [9.1, 8.4, 7.2]
    assert first["measurement_jitter_ms"] < second["measurement_jitter_ms"]


def test_a_configured_inflight_path_still_contributes_a_reading() -> None:
    """The documented workaround must not switch the data collection off.

    SP005's advice, when the fallback cannot resolve, is to point
    `--inflight-path` at a slower endpoint. Runs that take that advice are the
    ones most likely to come from a real deployment on a real host. Their
    readiness baseline is measured either way, so the ratio is the same evidence
    about the same machine — recording it only on the fallback path would leave
    the question open on exactly the hosts best placed to close it.
    """
    calibration = _document(_report(inflight_target="configured"))["runs"][0][
        "resolution_calibration"
    ]

    assert calibration["inflight_target"] == "configured"
    assert calibration["readiness_p50_ms"] is not None
    assert calibration["ratio"] is not None


def test_the_ratio_is_reported_against_the_constant_in_force() -> None:
    """A row is only comparable if it says which floor it was measured against."""
    calibration = _document(_report())["runs"][0]["resolution_calibration"]

    expected = calibration["readiness_p50_ms"] / calibration["measurement_jitter_ms"]
    assert calibration["ratio"] == round(expected, 3)
    assert calibration["minimum_ratio"] == MIN_JITTER_RATIO


def test_every_sample_is_kept_and_not_only_its_summary() -> None:
    """The samples are already paid for; discarding them is the only cost.

    p50 and max are enough to apply the rule in force and not enough to ask
    whether a different one would have been steadier. Answering that from
    summaries alone means running the whole prediction again N times, which on a
    realistic profile is minutes per data point — so the readings are written
    down instead, on the run that already took them.

    The jitter samples are the half that matters more: across the three-host
    campaign the ratio's volatility was almost entirely in its denominator.
    """
    calibration = _document(_report())["runs"][0]["resolution_calibration"]

    readings = calibration["readiness_latencies_ms"]
    assert len(readings) == calibration["readiness_samples"]
    assert calibration["readiness_min_ms"] == min(readings)
    assert calibration["readiness_max_ms"] == max(readings)

    jitter = calibration["measurement_jitter_latencies_ms"]
    assert len(jitter) == calibration["measurement_jitter_samples"]


def test_the_absolute_floor_travels_with_the_ratio_it_guards() -> None:
    """Both constants, or the row cannot be re-scored later.

    A batch is only comparable against a later threshold change if it says which
    thresholds it was measured under. The ratio has carried its own since this
    block existed; the floor added beside it has to as well, or old batches
    silently get re-read under whatever the constant becomes.
    """
    calibration = _document(_report())["runs"][0]["resolution_calibration"]

    assert calibration["minimum_ratio"] == MIN_JITTER_RATIO
    assert calibration["minimum_readiness_window_ms"] == MIN_READINESS_WINDOW_MS


def test_the_jitter_reading_names_where_it_was_measured() -> None:
    """Sidecar and host-fallback jitter are different quantities.

    The sidecar times TCP round trips to the target; the host fallback times the
    Docker daemon. Pooling them across hosts would compare a container-network
    figure against a socket-API one and call the difference a host difference.
    """
    calibration = _document(_report())["runs"][0]["resolution_calibration"]

    assert calibration["measurement_jitter_source"] == "sidecar"
    assert calibration["measurement_jitter_samples"] == 3


def test_the_host_id_groups_runs_by_machine_and_not_by_load() -> None:
    quiet = _report(load=(0.2, 0.2, 0.2))
    busy = _report(load=(11.0, 9.0, 7.0))

    runs = _document(quiet, busy)["runs"]

    ids = {run["resolution_calibration"]["host_id"] for run in runs}
    assert len(ids) == 1
    identifier = ids.pop()
    for fact in ("Darwin 25.5.0", "28.1.1", "10cpu"):
        assert fact in identifier


def test_the_host_id_is_still_a_key_when_the_facts_are_missing() -> None:
    """An unknown host groups with other unknown hosts, rather than crashing."""
    report = _report()
    report.docker_server = {}
    report.cpu_count = None
    report.host_os = ""

    identifier = _document(report)["runs"][0]["resolution_calibration"]["host_id"]

    assert "unknown" in identifier


def test_a_run_that_measured_nothing_records_nulls_rather_than_zeroes() -> None:
    """A missing reading must not enter the sample as a fast one."""
    report = _report(jitter_ns=(), readiness_ns=())
    report.readiness_baseline = None

    calibration = _document(report)["runs"][0]["resolution_calibration"]

    assert calibration["measurement_jitter_ms"] is None
    assert calibration["readiness_p50_ms"] is None
    assert calibration["ratio"] is None
    assert calibration["readiness_samples"] == 0
