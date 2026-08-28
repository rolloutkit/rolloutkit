"""Every matrix row that counts in-flight requests must have a window to count in.

SP005 reaches `all_completed` or `requests_destroyed` only by counting requests
that were connected before T0 and unfinished after it. If a fixture's window is
tight enough that a loaded runner can empty it, the row does not fail loudly —
it degrades to `nothing_in_flight`, an ERROR that looks like a defect in the
image under test rather than a defect in the fixture. That is the shape of every
`test: widen …` and `test: stabilize …` commit in this repository's history.

The gates below use the tool's own constants rather than numbers tuned to
today's runner, so a change to what rolloutkit considers resolvable moves the
fixtures with it. Configs are read through `load_config`, not parsed here, so
the check cannot drift from the durations the engine actually sees.

Rows whose SP005 verdict comes from a precondition — `disabled`,
`baseline_not_2xx`, `shutdown_never_started`, `nothing_in_flight` — are exempt:
they are decided before anything is counted, so an empty window cannot change
them. `nothing-in-flight` exists precisely to produce an empty one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rolloutkit.config.loader import load_config
from rolloutkit.contracts.inflight import MIN_JITTER_RATIO
from rolloutkit.traffic.accept_probe import ACCEPT_PROBE_INTERVAL_MS

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

#: The branches that are reached by counting. Only these depend on the window.
COUNT_DEPENDENT = ("all_completed", "requests_destroyed")

#: How much of the request has to survive T0. A window narrower than the tool's
#: own resolution floor — the interval it samples accepts at, times the ratio it
#: demands over measurement noise — is one whose boundary rolloutkit itself
#: would refuse to trust.
MIN_SURVIVING_WINDOW_MS = MIN_JITTER_RATIO * ACCEPT_PROBE_INTERVAL_MS


def _rows() -> list[dict]:
    matrix = yaml.safe_load((FIXTURES / "matrix.yaml").read_text())
    return [
        entry
        for entry in matrix["fixtures"]
        if (entry["expect"].get("SP005") or {}).get("branch") in COUNT_DEPENDENT
    ]


def _pinned_rows() -> list[dict]:
    """Count-dependent rows that pin `sigterm_after` to a wall-clock constant.

    A row that leaves it unset gets half the measured p50, so its window is
    derived on the same machine, in the same conditions, moments earlier — it
    tracks the runner instead of arguing with it. Only a pinned value can be
    wrong in a way a static check can see.
    """
    pinned = []
    for entry in _rows():
        config = load_config(config_path=FIXTURES / entry["config"])
        inflight = config.contracts.inflight
        if inflight is not None and inflight.sigterm_after is not None:
            pinned.append({**entry, "config_object": config})
    return pinned


def test_the_matrix_still_has_rows_that_count() -> None:
    """Guards the guard: an empty parametrisation would pass in silence."""
    assert _rows(), "no matrix row expects a counted SP005 branch any more"


@pytest.mark.parametrize("entry", _pinned_rows(), ids=lambda e: e["name"])
def test_a_pinned_signal_lands_while_the_request_is_still_running(
    entry: dict,
) -> None:
    inflight = entry["config_object"].contracts.inflight
    assert inflight is not None
    lead = inflight.sigterm_after
    duration = inflight.request.expected_duration
    assert lead is not None
    assert lead < duration, (
        f"{entry['name']}: the signal is aimed {lead}ms in, but the request only "
        f"lasts {duration}ms, so nothing can still be running at T0. This row "
        f"expects SP005 {entry['expect']['SP005']['branch']}, which is reached by "
        "counting in-flight requests; it would report nothing_in_flight instead. "
        "Lower contracts.inflight.sigterm_after, or leave it unset and let it be "
        "derived from the baseline p50."
    )


@pytest.mark.parametrize("entry", _pinned_rows(), ids=lambda e: e["name"])
def test_a_pinned_window_survives_t0_by_more_than_the_resolution_floor(
    entry: dict,
) -> None:
    inflight = entry["config_object"].contracts.inflight
    assert inflight is not None
    lead = inflight.sigterm_after
    duration = inflight.request.expected_duration
    assert lead is not None
    surviving = duration - lead
    assert surviving >= MIN_SURVIVING_WINDOW_MS, (
        f"{entry['name']}: only {surviving}ms of the request survives T0 "
        f"({duration}ms request, signal aimed {lead}ms in). Below "
        f"{MIN_SURVIVING_WINDOW_MS}ms — {MIN_JITTER_RATIO}x the "
        f"{ACCEPT_PROBE_INTERVAL_MS}ms accept-probe interval — a loaded runner "
        f"can empty the window, and this row's expected "
        f"{entry['expect']['SP005']['branch']} silently becomes an ERROR about "
        "the image instead of a failure about the fixture. Lengthen the "
        "endpoint the fixture calls rather than moving sigterm_after: the "
        "signal has to stay early enough to land mid-request."
    )
