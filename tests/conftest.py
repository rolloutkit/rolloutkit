"""Shared fixtures, and the per-row record of the Docker matrix.

`docs/ci-runs.md` records what a CI run was expected to prove. That is a
forecast, and a forecast cannot settle the question that actually comes up when
a matrix row goes red: *was this row green before, and on what?* A red run tells
you the row failed once; nothing on disk tells you whether the fifty runs before
it passed, or whether the row has been flipping for a week on one host.

So every row writes what it observed — expected against actual, per contract,
with the exit code and the identity of the run — to a JSON Lines file that CI
uploads as an artifact. One line per row, self-contained, so artifacts from
different runs can be concatenated and read with `jq` without a schema.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Where the record goes. Overridable so a local run can keep a batch of its own
#: without clobbering the last one; CI leaves it at the default and uploads that.
MATRIX_LOG_ENV = "ROLLOUTKIT_MATRIX_LOG"
DEFAULT_MATRIX_LOG = ROOT / "matrix-results.jsonl"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _docker_version() -> str:
    binary = shutil.which("docker")
    if binary is None:
        return ""
    probe = subprocess.run(
        [binary, "version", "--format", "{{.Server.Version}}/{{.Server.Os}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _commit() -> str:
    """The commit under test, from CI if it said so and from git otherwise."""
    for name in ("ROLLOUTKIT_COMMIT", "GITHUB_SHA"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return head.stdout.strip() if head.returncode == 0 else ""


_RUN: dict[str, object] | None = None


def _run_identity() -> dict[str, object]:
    """Everything needed to tell two runs of the same row apart. Computed once."""
    global _RUN
    if _RUN is None:
        _RUN = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": _commit(),
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
            "docker": _docker_version(),
            # Empty off CI. Present, these are what turns "it was green before"
            # into a link: run 32991685197 attempt 1 is a page, not a memory.
            "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "github_job": os.environ.get("GITHUB_JOB", ""),
        }
    return _RUN


@pytest.fixture
def matrix_row() -> dict:
    """Where a matrix row leaves what it saw, for the recorder below.

    A fixture rather than a return value because the interesting rows are the
    ones that fail: an assertion stops the test before it could hand anything
    back, and the observation that caused the failure is exactly the one worth
    keeping. What the test puts in here survives its own failure.
    """
    return {}


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    report = yield
    # `call` is the test body. A skip decided during setup has no observation to
    # record — no CLI was ever invoked — and a teardown error does not change
    # the verdict the row produced.
    if report.when != "call":
        return report
    observed = getattr(item, "funcargs", {}).get("matrix_row")
    if not observed:
        return report
    _append(
        {
            "run": _run_identity(),
            "test": item.nodeid,
            "outcome": report.outcome,
            "duration_s": round(report.duration, 3),
            **observed,
        }
    )
    return report


def _append(record: dict) -> None:
    path = Path(os.environ.get(MATRIX_LOG_ENV) or DEFAULT_MATRIX_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Appended per row rather than written at the end of the session: a job that
    # is cancelled or killed mid-matrix still leaves the rows it finished, and
    # those are the runs a flake investigation is short of.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start each session's record empty, so a file is one session's evidence."""
    path = Path(os.environ.get(MATRIX_LOG_ENV) or DEFAULT_MATRIX_LOG)
    if path.exists():
        path.unlink()
    # Nothing is created here: a session that runs no matrix row should leave no
    # record at all, rather than an empty file that reads as a matrix of zero
    # rows all of which passed.
