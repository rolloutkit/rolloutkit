"""A run that prints nothing is indistinguishable from a run that has hung.

The measurement itself is mostly waiting: a 25-request baseline against a slow
endpoint takes as long as the endpoint does, and a cold probe image adds a pull
on top. Before this, the only thing a user ever saw was the pull line — so the
best case, a warm cache, was also the most silent one.
"""

from __future__ import annotations

from rolloutkit.engine.lifecycle import _begin_phase
from rolloutkit.engine.runner import _run_progress


def test_a_phase_announces_itself_and_stamps_its_start() -> None:
    seen: list[str] = []

    started_ns = _begin_phase(seen.append, "measuring the baseline")

    assert seen == ["measuring the baseline"]
    assert started_ns > 0


def test_a_phase_is_still_timed_when_nobody_is_listening() -> None:
    """Embedded use and the test suite pass no callback; timing must not care."""
    assert _begin_phase(None, "measuring the baseline") > 0


def test_a_single_run_is_not_numbered() -> None:
    seen: list[str] = []

    progress = _run_progress(seen.append, 0, 1)
    progress("starting the traffic probe")

    assert seen == ["starting the traffic probe"]


def test_repeats_are_numbered_so_they_do_not_read_as_one_restarting_run() -> None:
    seen: list[str] = []

    for index in range(3):
        _run_progress(seen.append, index, 3)("sending SIGTERM")

    assert seen == [
        "run 1/3: sending SIGTERM",
        "run 2/3: sending SIGTERM",
        "run 3/3: sending SIGTERM",
    ]


def test_no_callback_stays_no_callback() -> None:
    assert _run_progress(None, 0, 3) is None
