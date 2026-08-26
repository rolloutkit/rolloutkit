"""Turn a directory of JSON reports into a table you can compare across hosts.

Reads whatever `measure-runs.sh` left behind and prints three blocks: where the
prediction spent its wall clock, what the host could resolve while it did, and
what the teardown floor measured. Each ends with a median row, because a single
run of any of these says very little — the point of the exercise is the spread.

Standard library only, and no assumptions about which fields a run managed to
fill in. A run that measured nothing prints dashes rather than zeroes: a missing
reading that enters the table as 0.0 is a fast one, and it would pull the median
of the very quantity the table exists to establish.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

DASH = "-"


def _load(directory: Path) -> list[dict[str, Any]]:
    documents = []
    for path in sorted(directory.glob("run-*.json")):
        text = path.read_text().strip()
        if not text:
            # Exit 2 or 3: the run stopped before a report was written.
            documents.append({"_file": path.name, "_empty": True})
            continue
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            documents.append({"_file": path.name, "_empty": True, "_error": str(error)})
            continue
        document["_file"] = path.name
        documents.append(document)
    return documents


def _wall(directory: Path) -> dict[int, tuple[int, int]]:
    """Per-run exit code and wall time, as the shell measured them."""
    path = directory / "wall.tsv"
    if not path.is_file():
        return {}
    wall = {}
    for line in path.read_text().splitlines():
        index, status, elapsed = line.split("\t")
        wall[int(index)] = (int(status), int(elapsed))
    return wall


def _runs(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document.get("runs") or []


def _fmt(value: Any, places: int = 1) -> str:
    if value is None:
        return DASH
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


def _table(title: str, headers: list[str], rows: list[list[str]], note: str = "") -> None:
    print(f"\n{title}")
    if note:
        print(f"  {note}")
    if not rows:
        print("  (nothing measured)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]
    line = "  ".join(h.rjust(w) for h, w in zip(headers, widths))
    print(f"  {line}")
    print(f"  {'  '.join('-' * w for w in widths)}")
    for row in rows:
        print(f"  {'  '.join(cell.rjust(w) for cell, w in zip(row, widths))}")


def _median(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: summarise_runs.py DIRECTORY", file=sys.stderr)
        return 2
    directory = Path(argv[1])
    if not directory.is_dir():
        print(f"summarise_runs: no such directory: {directory}", file=sys.stderr)
        return 2

    documents = _load(directory)
    wall = _wall(directory)
    measured = [d for d in documents if not d.get("_empty")]
    if not documents:
        print(f"summarise_runs: no run-*.json in {directory}", file=sys.stderr)
        return 1

    # The host names itself. If a batch somehow spans two, say so rather than
    # printing one of them and letting the file be read as a single machine.
    identities = {
        run["resolution_calibration"]["host_id"]
        for document in measured
        for run in _runs(document)
        if run.get("resolution_calibration")
    }
    images = {(d.get("target") or {}).get("image") for d in measured}
    versions = {
        f"{d['tool_version']} ({(d.get('preflightkit_commit') or '?')[:7]})"
        for d in measured
        if d.get("tool_version")
    }

    print(f"host:   {' / '.join(sorted(identities)) or DASH}")
    if len(identities) > 1:
        print("        WARNING: these runs are from more than one host — do not pool them")
    print(f"image:  {' '.join(sorted(i for i in images if i)) or DASH}")
    print(f"tool:   {' '.join(sorted(versions)) or DASH}")
    print(f"runs:   {len(documents)} attempted, {len(measured)} with a report")
    batch = directory / "batch.txt"
    if batch.is_file():
        for line in batch.read_text().splitlines():
            if line.startswith(("command:", "label:")):
                key, value = line.split(":", 1)
                print(f"{key + ':':<9}{value.strip()}")

    phases: list[str] = []
    for document in measured:
        for run in _runs(document):
            for phase in run.get("phase_durations_ms") or {}:
                if phase not in phases:
                    phases.append(phase)

    duration_rows: list[list[str]] = []
    columns: dict[str, list[float | None]] = {key: [] for key in ["wall", "total", *phases]}
    for index, document in enumerate(documents, start=1):
        status, elapsed = wall.get(index, (None, None))
        if document.get("_empty"):
            duration_rows.append(
                [str(index), _fmt(status), _fmt(elapsed and elapsed / 1000, 2), *[DASH] * (1 + len(phases))]
            )
            continue
        durations = document.get("phase_durations_ms") or {}
        total = document.get("duration_ms")
        values = [durations.get(phase) for phase in phases]
        columns["wall"].append(elapsed / 1000 if elapsed is not None else None)
        columns["total"].append(total / 1000 if total is not None else None)
        for phase, value in zip(phases, values):
            columns[phase].append(value)
        duration_rows.append(
            [
                str(index),
                _fmt(status),
                _fmt(elapsed / 1000 if elapsed is not None else None, 2),
                _fmt(total / 1000 if total is not None else None, 2),
                *[_fmt(v, 0) for v in values],
            ]
        )
    duration_rows.append(
        [
            "med",
            "",
            _fmt(_median(columns["wall"]), 2),
            _fmt(_median(columns["total"]), 2),
            *[_fmt(_median(columns[phase]), 0) for phase in phases],
        ]
    )
    _table(
        "duration",
        ["run", "exit", "wall s", "tool s", *phases],
        duration_rows,
        "wall is the whole process; tool is what the run itself timed; phases in ms",
    )

    resolution_rows: list[list[str]] = []
    resolution: dict[str, list[float | None]] = {k: [] for k in ["jitter", "p50", "ratio"]}
    minimum = set()
    for index, document in enumerate(documents, start=1):
        for position, run in enumerate(_runs(document), start=1):
            calibration = run.get("resolution_calibration") or {}
            if not calibration:
                continue
            jitter = calibration.get("measurement_jitter_ms")
            p50 = calibration.get("readiness_p50_ms")
            ratio = calibration.get("ratio")
            resolution["jitter"].append(jitter)
            resolution["p50"].append(p50)
            resolution["ratio"].append(ratio)
            if calibration.get("minimum_ratio") is not None:
                minimum.add(calibration["minimum_ratio"])
            load = calibration.get("load_average")
            resolution_rows.append(
                [
                    f"{index}.{position}" if len(_runs(document)) > 1 else str(index),
                    _fmt(jitter, 3),
                    str(calibration.get("measurement_jitter_source") or DASH),
                    str(calibration.get("measurement_jitter_samples") or DASH),
                    _fmt(p50, 2),
                    _fmt(calibration.get("readiness_max_ms"), 2),
                    _fmt(ratio, 2),
                    "yes" if ratio is not None and calibration.get("minimum_ratio") is not None
                    and ratio >= calibration["minimum_ratio"] else "no" if ratio is not None else DASH,
                    str(calibration.get("inflight_target") or DASH),
                    _fmt(load[0] if load else None, 2),
                ]
            )
    if resolution_rows:
        resolution_rows.append(
            [
                "med",
                _fmt(_median(resolution["jitter"]), 3),
                "",
                "",
                _fmt(_median(resolution["p50"]), 2),
                "",
                _fmt(_median(resolution["ratio"]), 2),
                "",
                "",
                "",
            ]
        )
    _table(
        "resolution",
        ["run", "jitter", "source", "n", "p50", "max", "ratio", "ok", "target", "load1"],
        resolution_rows,
        f"ok is ratio >= {sorted(minimum)[0] if minimum else '?'} (MIN_JITTER_RATIO as this build has it)",
    )

    teardown_rows: list[list[str]] = []
    floors: list[float | None] = []
    for index, document in enumerate(documents, start=1):
        for position, run in enumerate(_runs(document), start=1):
            calibration = run.get("teardown_calibration") or {}
            status = run.get("teardown_calibration_status") or DASH
            floors.append(calibration.get("floor_ms"))
            teardown_rows.append(
                [
                    f"{index}.{position}" if len(_runs(document)) > 1 else str(index),
                    str(calibration.get("sample_count") or DASH),
                    _fmt(calibration.get("floor_ms"), 2),
                    _fmt(calibration.get("stddev_ms"), 2),
                    _fmt(calibration.get("stddev_k"), 1),
                    _fmt(calibration.get("resolution_threshold_ms"), 2),
                    str(status),
                ]
            )
    if teardown_rows:
        teardown_rows.append(["med", "", _fmt(_median(floors), 2), "", "", "", ""])
    _table(
        "teardown floor",
        ["run", "n", "floor", "stddev", "k", "threshold", "status"],
        teardown_rows,
        "the other half of the same question: what this daemon costs to stop, in "
        "ms. A run only calibrates when a budget is close enough to need it, so "
        "not_calibrated is a normal row, not a missing measurement",
    )

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
