"""The harness that will be carried to the other two hosts.

`scripts/measure-runs.sh` and `scripts/summarise_runs.py` exist to make one
comparison possible: the same prediction, run on a Linux server, a macOS laptop
and a CI runner, printed in a table nobody has to transcribe. A crash on the
second host does not cost a table, it costs the trip — the machine may not be
available again, and the numbers were never written down anywhere else.

So these tests are about survival rather than beauty. They feed the summariser
the shapes a run can actually produce, including the ones where a run stopped
before it measured anything, and check that it prints something and does not
silently turn a missing reading into a fast one.

No Docker. The documents are built here, which is the point: a real run cannot
be arranged on demand on the host where this would break.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "measure-runs.sh"


def _summariser():
    spec = importlib.util.spec_from_file_location(
        "summarise_runs", ROOT / "scripts" / "summarise_runs.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _document(
    *,
    jitter: float | None = 1.25,
    p50: float | None = 5.0,
    host: str = "Linux 6.8.0 / docker 27.0.3 / 8cpu",
    load: list[float] | None = None,
) -> dict[str, Any]:
    ratio = p50 / jitter if p50 is not None and jitter else None
    return {
        "tool_version": "0.1.0",
        "preflightkit_commit": "0123456789abcdef",
        "target": {"image": "example:latest", "port": 8000},
        "duration_ms": 38_000.0,
        "phase_durations_ms": {"target_start": 480.0, "calibration": 640.0},
        "runs": [
            {
                "duration_ms": 38_000.0,
                "phase_durations_ms": {"target_start": 480.0, "calibration": 640.0},
                "resolution_calibration": {
                    "host_id": host,
                    "load_average": load,
                    "measurement_jitter_ms": jitter,
                    "measurement_jitter_source": "sidecar",
                    "measurement_jitter_samples": 5,
                    "readiness_p50_ms": p50,
                    "readiness_max_ms": 9.0,
                    "readiness_samples": 5,
                    "ratio": ratio,
                    "minimum_ratio": 10,
                    "inflight_target": "readiness_fallback",
                },
                "teardown_calibration_status": "not_calibrated",
                "teardown_calibration": None,
            }
        ],
    }


def _batch(
    directory: Path,
    *documents: dict[str, Any] | None,
    wall_ms: tuple[int, ...] = (),
) -> Path:
    lines = []
    for index, document in enumerate(documents, start=1):
        path = directory / f"run-{index:02d}.json"
        # None is a run that exited before writing: the shell still created the
        # file by redirecting into it, so an empty file is a real input here.
        path.write_text("" if document is None else json.dumps(document))
        elapsed = wall_ms[index - 1] if index <= len(wall_ms) else 40_000 + index
        lines.append(f"{index}\t{0 if document else 3}\t{elapsed}")
    (directory / "wall.tsv").write_text("\n".join(lines) + "\n")
    return directory


def test_a_batch_prints_the_three_blocks(tmp_path: Path, capsys) -> None:
    _batch(tmp_path, _document(), _document(jitter=0.4, p50=6.0))

    assert _summariser().main(["summarise_runs.py", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    for block in ("duration", "resolution", "teardown floor"):
        assert f"\n{block}\n" in out
    assert "Linux 6.8.0 / docker 27.0.3 / 8cpu" in out
    assert "example:latest" in out
    # The median row is the reason for running more than once.
    assert out.count("\n  med") == 3


def test_a_run_that_measured_nothing_does_not_become_a_fast_one(
    tmp_path: Path, capsys
) -> None:
    """The failure that would quietly corrupt a host's numbers.

    A run that exits before measuring leaves an empty document. Counted as zero
    it is the fastest run in the batch, and it drags the median of the very
    quantity the trip was made to establish.
    """
    # The two runs that measured took 30s and 50s; the one that died took 1s.
    # Counting it, the median wall time is 30.00 instead of 40.00.
    _batch(tmp_path, _document(), None, _document(), wall_ms=(30_000, 1_000, 50_000))

    assert _summariser().main(["summarise_runs.py", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "3 attempted, 2 with a report" in out
    duration = out.split("\nduration\n", 1)[1].split("\nresolution\n", 1)[0]
    median = next(line for line in duration.splitlines() if line.startswith("  med"))
    assert median.split() == ["med", "40.00", "38.00", "480", "640"], median
    # The dead run is still listed, so the batch cannot be read as three clean
    # ones — it is dropped from the statistics, not from the record.
    assert any(line.split()[:3] == ["2", "3", "1.00"] for line in duration.splitlines())


def test_two_hosts_in_one_directory_are_called_out(tmp_path: Path, capsys) -> None:
    """Pooling two machines is the one mistake this whole exercise is about."""
    _batch(tmp_path, _document(), _document(host="Darwin 25.5.0 / docker 29.7.2 / 11cpu"))

    _summariser().main(["summarise_runs.py", str(tmp_path)])

    assert "more than one host" in capsys.readouterr().out


def test_a_missing_reading_prints_as_absent_not_as_zero(tmp_path: Path, capsys) -> None:
    _batch(tmp_path, _document(jitter=None, p50=None, load=None))

    _summariser().main(["summarise_runs.py", str(tmp_path)])

    resolution = capsys.readouterr().out.split("\nresolution\n", 1)[1]
    row = next(line for line in resolution.splitlines() if line.strip().startswith("1"))
    assert row.split() == ["1", "-", "sidecar", "5", "-", "9.00", "-", "-", "readiness_fallback", "-"]


def test_a_corrupt_document_is_reported_not_raised(tmp_path: Path, capsys) -> None:
    (tmp_path / "run-01.json").write_text("{not json")

    assert _summariser().main(["summarise_runs.py", str(tmp_path)]) == 0

    assert "1 attempted, 0 with a report" in capsys.readouterr().out


def test_an_empty_directory_says_so(tmp_path: Path, capsys) -> None:
    assert _summariser().main(["summarise_runs.py", str(tmp_path)]) == 1


def test_the_checkout_path_is_not_printed_into_a_committed_summary(
    tmp_path: Path, capsys
) -> None:
    """These summaries are committed; a checkout path is not a measurement.

    `measure-runs.sh` records the command as it actually ran, absolute path and
    all, because the run has to be reproducible. On a laptop that path carries a
    username and whatever the directory was called locally, and on CI it carries
    the runner's layout. The batch file keeps it; the summary does not.
    """
    _batch(tmp_path, _document())
    (tmp_path / "batch.txt").write_text(
        "command: uv run --project /Users/someone/projects/internal-name "
        "preflightkit test --config "
        "/Users/someone/projects/internal-name/fixtures/a.yaml\n"
        "label: fallback\n"
        "uname: Darwin someones-laptop.local 25.5.0\n"
    )

    assert _summariser().main(["summarise_runs.py", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "/Users/someone" not in out
    assert "internal-name" not in out
    assert "--project . " in out
    assert "--config fixtures/a.yaml" in out
    # `uname` carries the machine's own name and was never printed. Asserted so
    # that widening the printed keys has to come past this.
    assert "someones-laptop" not in out
    assert "label:" in out


def test_a_batch_recorded_without_project_is_left_alone(tmp_path: Path, capsys) -> None:
    """No `--project` means nothing identified the checkout.

    Guessing at absolute paths without one would eat real arguments — a config
    path, a mounted volume — and silently change what the summary says was run.
    """
    _batch(tmp_path, _document())
    (tmp_path / "batch.txt").write_text(
        "command: preflightkit test --format json example:latest --port 8000\n"
        "label: fast\n"
    )

    assert _summariser().main(["summarise_runs.py", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "preflightkit test --format json example:latest --port 8000" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell harness")
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], "nothing to run"),
        (["-c", "/no/such/config.yaml"], "no such config"),
        (["-n", "0", "-c", "pyproject.toml"], "positive integer"),
        (["-Z"], "unknown option"),
        (["-n"], "needs a value"),
    ],
)
def test_the_harness_refuses_a_batch_it_cannot_run(argv: list[str], expected: str) -> None:
    """Refusing before the first run, rather than after the fifth.

    Every one of these is a typo someone makes at 9pm on a borrowed Linux box.
    Finding out at the end of the batch means running it again.
    """
    run = subprocess.run(
        ["bash", str(HARNESS), *argv], cwd=ROOT, capture_output=True, text=True, timeout=60
    )

    assert run.returncode == 2, run.stdout
    assert expected in run.stderr, run.stderr


def test_the_harness_explains_itself_without_running_anything() -> None:
    run = subprocess.run(
        ["bash", str(HARNESS), "-h"], cwd=ROOT, capture_output=True, text=True, timeout=60
    )

    assert run.returncode == 0
    for flag in ("-c FILE", "-n N", "-o DIR", "-l LABEL"):
        assert flag in run.stdout, run.stdout
