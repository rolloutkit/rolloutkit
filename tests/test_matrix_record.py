"""The Docker matrix has to leave a per-row record, and it has to survive red runs.

The wiring is two pieces that can be broken independently and silently: the
matrix row has to ask for the `matrix_row` fixture and put its observations in
it, and `.github/workflows/ci.yml` has to upload the file whatever the suite's
verdict was. Neither shows up in the fast suite's output, and neither is missed
until the day a row goes red and there is nothing to compare it against —
which, being the day the record was needed, is the worst day to discover it.

This file runs without a daemon, so a broken recorder fails on every push
rather than only on the runs that build images.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import yaml

import conftest
import test_fixtures as matrix_module

ROOT = Path(__file__).resolve().parent.parent


def test_the_matrix_row_asks_for_the_recorder() -> None:
    row = matrix_module.test_fixture_matches_the_matrix
    parameters = inspect.signature(row).parameters
    assert "matrix_row" in parameters, (
        "the matrix row stopped requesting `matrix_row`, so the Docker job now "
        "produces an empty artifact and says nothing about any row"
    )


def test_a_row_that_fails_is_still_recorded(tmp_path, monkeypatch) -> None:
    """The point of the record: red runs are the ones worth keeping.

    The hook reads what the row left behind rather than what it returned, so an
    assertion that stops the test mid-way still publishes the observation that
    caused it. Written here against the recorder itself; the hook's own
    behaviour on a failing test is exercised by the matrix.
    """
    log = tmp_path / "rows.jsonl"
    monkeypatch.setenv(conftest.MATRIX_LOG_ENV, str(log))
    conftest._append({"row": "synthetic", "outcome": "failed", "exit_code": 1})
    conftest._append({"row": "synthetic-2", "outcome": "passed", "exit_code": 0})

    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["row"] for r in records] == ["synthetic", "synthetic-2"], (
        "records are appended as rows finish, so a cancelled job keeps what it "
        "already ran"
    )


def test_the_record_carries_enough_to_tell_two_runs_apart() -> None:
    """A row without the run it came from cannot answer 'was it green before'."""
    identity = conftest._run_identity()
    assert {"at", "commit", "os", "cpus", "docker"} <= identity.keys()
    assert "github_run_id" in identity and "github_run_attempt" in identity


def test_the_record_keeps_the_numbers_a_contract_judged() -> None:
    """A verdict says which side of the line a row fell on, never by how much.

    The margin is the whole question when a row goes red on a slow host: a
    measurement 930ms from its FAIL boundary and one 4.9s from it are the same
    verdict and a different fixture. Reconstructing it after the fact needs the
    budget from the fixture and the duration parsed out of an English sentence,
    and for `startup-within-resolution` it is not possible at all — the band is
    measured per run and published nowhere else.
    """
    observed = matrix_module._observed(
        {
            "SP006": {
                "status": "WARN",
                "branch": "thin_margin",
                "summary": "exited in 25.11s, leaving only 4.89s of the 30s budget",
                "actual": {"shutdown_duration_ms": 25110, "margin_ms": 4890},
                "evidence": {"per_request": ["one entry per request driven"]},
            }
        }
    )["SP006"]

    assert observed["measured"]["margin_ms"] == 4890, (
        "the record dropped the numbers the contract judged, so a red row can "
        "only be compared by re-reading its summary in English"
    )
    assert "evidence" not in observed, (
        "evidence grows with the traffic a row drives; one bounded line per row "
        "is what makes these artifacts concatenable"
    )


def test_ci_uploads_the_record_even_when_the_matrix_fails() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["docker"]["steps"]
    upload = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    assert len(upload) == 1, "the Docker job no longer uploads exactly one artifact"
    step = upload[0]
    # `if: always()` is the whole mechanism. Without it the artifact exists only
    # for the runs nobody needs to investigate.
    assert str(step.get("if")).strip() == "always()", (
        "the matrix record is uploaded conditionally, so a failing matrix — the "
        "only run the record was built for — would upload nothing"
    )
    assert step["with"]["path"] == conftest.DEFAULT_MATRIX_LOG.name
