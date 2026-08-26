"""Ask how repeatable the fallback resolution decision is, not what it decides.

SP005 falls back to readiness when no in-flight path is configured, and then has
to decide whether the window it measured can be told apart from the transport's
own noise. The rule is one number: `readiness_p50 / jitter >= MIN_JITTER_RATIO`,
evaluated on a single run.

The question this asks is deliberately not "is 10 the right number". A threshold
is only worth arguing about once the quantity under it is stable, and eight runs
of one configuration on one macOS host have already produced ratios from 1.22 to
16.08 — a rule applied to that spread is a coin toss with a threshold painted on
it, and moving the threshold moves which side the coin lands on more often
without making it land the same way twice.

So the metric here is disagreement: given the readings actually observed for one
configuration on one host, how often would two runs of it reach opposite
conclusions? That needs no ground truth, which matters, because there is no
honest way to label these batches "should have resolved" without assuming the
answer the analysis is supposed to produce.

Two candidate rules are measured against the same readings:

  ratio-k    the current comparison, but over k runs, resolving only when the
             lower end of the ratio's own spread clears the threshold. Where the
             spread straddles it, the answer is INCONCLUSIVE rather than
             whichever side this run happened to land on.
  p50-T      an absolute floor on readiness p50 in milliseconds, with the ratio
             dropped. Justified only if p50 is the steadier of the two terms,
             which is a claim about the data and is reported alongside.

Usage:  analyse_resolution.py DIRECTORY [DIRECTORY ...]
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any, NamedTuple

#: Resamples per estimate. Large enough that the third digit is stable, seeded
#: so that a number quoted in a field note can be reproduced exactly.
RESAMPLES = 20_000
SEED = 20260826

#: The rule in force, repeated rather than imported: this script is run against
#: readings taken by other builds, and importing the constant would silently
#: re-evaluate old batches under a new threshold.
CURRENT_RATIO = 10.0

RATIO_SAMPLE_COUNTS = (1, 3, 5, 9)
ABSOLUTE_THRESHOLDS_MS = (5.0, 10.0, 20.0, 50.0)


class Reading(NamedTuple):
    jitter_ms: float
    p50_ms: float
    ratio: float
    target: str


class Batch(NamedTuple):
    host: str
    label: str
    readings: list[Reading]


def _load(directory: Path) -> list[Batch]:
    batches: list[Batch] = []
    for batch_dir in sorted(p for p in directory.rglob("*") if p.is_dir()):
        documents = sorted(batch_dir.glob("run-*.json"))
        if not documents:
            continue
        readings: list[Reading] = []
        hosts: set[str] = set()
        for path in documents:
            try:
                document = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for run in document.get("runs") or []:
                calibration = run.get("resolution_calibration") or {}
                jitter = calibration.get("measurement_jitter_ms")
                p50 = calibration.get("readiness_p50_ms")
                ratio = calibration.get("ratio")
                if jitter is None or p50 is None or ratio is None:
                    continue
                hosts.add(str(calibration.get("host_id")))
                readings.append(
                    Reading(jitter, p50, ratio, str(calibration.get("inflight_target")))
                )
        if readings:
            host = hosts.pop() if len(hosts) == 1 else f"{len(hosts)} hosts"
            batches.append(Batch(host, batch_dir.name, readings))
    return batches


def _spread(values: list[float]) -> tuple[float, float, float, float]:
    """Median, min, max, and coefficient of variation."""
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return (
        statistics.median(values),
        min(values),
        max(values),
        deviation / mean if mean else 0.0,
    )


def _disagreement(probability: float) -> float:
    """Chance two independent applications of a rule reach opposite answers."""
    return 2 * probability * (1 - probability)


def _ratio_rule(sample: list[float], threshold: float) -> str:
    """Resolve only if the whole sample clears the threshold.

    The lower end of the observed spread is the bound, rather than a parametric
    interval: these are eight-run batches whose ratios span an order of
    magnitude, and a normal approximation over that would report a confidence it
    has not earned.
    """
    if min(sample) >= threshold:
        return "resolve"
    if max(sample) < threshold:
        return "below"
    return "inconclusive"


def _estimate(readings: list[Reading], rng: random.Random) -> dict[str, Any]:
    ratios = [r.ratio for r in readings]
    p50s = [r.p50_ms for r in readings]
    result: dict[str, Any] = {}

    single = sum(1 for value in ratios if value >= CURRENT_RATIO) / len(ratios)
    result["current"] = {"resolve": single, "disagree": _disagreement(single)}

    for k in RATIO_SAMPLE_COUNTS:
        counts = {"resolve": 0, "inconclusive": 0, "below": 0}
        for _ in range(RESAMPLES):
            sample = [rng.choice(ratios) for _ in range(k)]
            counts[_ratio_rule(sample, CURRENT_RATIO)] += 1
        resolve = counts["resolve"] / RESAMPLES
        result[f"ratio-{k}"] = {
            "resolve": resolve,
            "inconclusive": counts["inconclusive"] / RESAMPLES,
            "disagree": _disagreement(resolve),
        }

    for threshold in ABSOLUTE_THRESHOLDS_MS:
        resolve = sum(1 for value in p50s if value >= threshold) / len(p50s)
        result[f"p50-{threshold:g}"] = {
            "resolve": resolve,
            "disagree": _disagreement(resolve),
        }
    return result


def _report(batches: list[Batch]) -> None:
    rng = random.Random(SEED)

    print("readings")
    print("  CV is the spread relative to the mean; the larger it is, the less a")
    print("  single run of that quantity tells you about the next one")
    header = (
        f"  {'host':<38} {'batch':<12} {'n':>2} {'target':<18} "
        f"{'jitter med':>10} {'cv':>6} {'p50 med':>9} {'cv':>6} "
        f"{'ratio med':>9} {'ratio min':>9} {'ratio max':>9} {'cv':>6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for batch in batches:
        jitter = _spread([r.jitter_ms for r in batch.readings])
        p50 = _spread([r.p50_ms for r in batch.readings])
        ratio = _spread([r.ratio for r in batch.readings])
        targets = {r.target for r in batch.readings}
        target = targets.pop() if len(targets) == 1 else "mixed"
        print(
            f"  {batch.host[:38]:<38} {batch.label:<12} {len(batch.readings):>2} "
            f"{target:<18} {jitter[0]:>10.3f} {jitter[3]:>6.2f} {p50[0]:>9.2f} "
            f"{p50[3]:>6.2f} {ratio[0]:>9.2f} {ratio[1]:>9.2f} {ratio[2]:>9.2f} "
            f"{ratio[3]:>6.2f}"
        )

    print()
    print("disagreement — chance two runs of the same configuration answer differently")
    print("  0.00 means settled; 0.50 is the worst a two-way rule can be")
    columns = (
        ["current"]
        + [f"ratio-{k}" for k in RATIO_SAMPLE_COUNTS]
        + [f"p50-{t:g}" for t in ABSOLUTE_THRESHOLDS_MS]
    )
    header = f"  {'host':<38} {'batch':<12} " + " ".join(f"{c:>12}" for c in columns)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for batch in batches:
        estimate = _estimate(batch.readings, rng)
        cells = " ".join(f"{estimate[c]['disagree']:>12.2f}" for c in columns)
        print(f"  {batch.host[:38]:<38} {batch.label:<12} {cells}")

    print()
    print("resolve rate — how often each rule says the window is measurable")
    print("  for ratio-k, the remainder is INCONCLUSIVE rather than a firm no")
    print(header)
    print("  " + "-" * (len(header) - 2))
    rng = random.Random(SEED)
    for batch in batches:
        estimate = _estimate(batch.readings, rng)
        cells = " ".join(f"{estimate[c]['resolve']:>12.2f}" for c in columns)
        print(f"  {batch.host[:38]:<38} {batch.label:<12} {cells}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: analyse_resolution.py DIRECTORY [DIRECTORY ...]", file=sys.stderr)
        return 2
    batches: list[Batch] = []
    for name in argv[1:]:
        directory = Path(name)
        if not directory.is_dir():
            print(f"analyse_resolution: no such directory: {directory}", file=sys.stderr)
            return 2
        batches.extend(_load(directory))
    if not batches:
        print("analyse_resolution: no readings found", file=sys.stderr)
        return 2
    _report(batches)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
