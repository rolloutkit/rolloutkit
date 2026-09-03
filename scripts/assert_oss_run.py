"""Fail unless the Paperless-ngx run matched what three local runs agreed on.

Run by the `oss-app` CI job, against the one fixture in this repository whose
image nobody here wrote.

Every other fixture is a container this repository builds, which means the
suite could stay entirely green while the pipeline failed on the first real
application anybody pointed it at: a third-party image brings an init system,
its own dependencies, migrations at startup, and a shutdown sequence nobody
here designed to be measurable. This job is the only thing standing between
that claim and nobody having checked it.

What it compares is deliberately narrow. `fixtures/oss/paperless-ngx/
expected.yaml` pins the exit code, the overall result, where the measurement
was taken from, that both dependency gates waited, and the branch each contract
took — and pins no duration at all. A millisecond measured on an eleven-core
laptop is not a prediction about a two-core runner, and a job that asserts one
reports the weather.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: assert_oss_run.py REPORT.json EXPECTED.yaml EXIT_CODE",
            file=sys.stderr,
        )
        return 2

    report = json.loads(Path(argv[1]).read_text())
    expected = yaml.safe_load(Path(argv[2]).read_text())
    exit_code = int(argv[3])

    failures = [
        *_check_result(report, expected, exit_code),
        *_check_probe_location(report, expected),
        *_check_dependency_gates(report, expected),
        *_check_contracts(report, expected),
    ]
    if not failures:
        print(
            f"{report.get('result')} as expected, measured from "
            f"{(report.get('environment') or {}).get('probe_location')}, "
            f"{len(expected.get('contracts') or {})} contracts on their expected "
            "branches, both dependency gates connected."
        )
        return 0

    print(
        f"The Paperless-ngx run did not match {argv[2]}.\n"
        "Either this repository changed what it measures, or upstream changed "
        "what it does — and upstream can only have changed in a commit that "
        "moved the digest in the fixture.\n",
        file=sys.stderr,
    )
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    return 1


def _check_result(
    report: dict[str, Any], expected: dict[str, Any], exit_code: int
) -> list[str]:
    """The verdict and what the shell was told about it.

    Two pins rather than one, because they are two different claims. `result`
    is what the contracts added up to; the exit code is the --fail-on policy
    applied to that sum, and it lives nowhere in the report — CI has to carry
    it in. A change to the default policy would leave `result` untouched and
    silently start failing, or stop failing, every pipeline that shells out.
    """
    failures = []
    if report.get("result") != expected["result"]:
        failures.append(
            f"result is {report.get('result')!r}, expected "
            f"{expected['result']!r}. Contract statuses below say which "
            "contract moved."
        )
    if exit_code != expected["exit_code"]:
        failures.append(
            f"rolloutkit exited {exit_code}, expected {expected['exit_code']}. "
            f"With result {report.get('result')!r} under the default --fail-on, "
            "this is the policy that changed, not the measurement."
        )
    return failures


def _check_probe_location(
    report: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    environment = report.get("environment") or {}
    location = environment.get("probe_location")
    if location == expected["probe_location"]:
        return []
    return [
        f"probe_location is {location!r}, expected "
        f"{expected['probe_location']!r} (fallback reason: "
        f"{environment.get('probe_fallback_reason')}). The step down to host "
        "traffic is correct behaviour on a host that cannot run a sidecar; a "
        "CI runner is not one, so here it means the sidecar broke."
    ]


def _check_dependency_gates(
    report: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    waits = {wait["service"]: wait for wait in report.get("dependency_waits") or []}
    failures = []
    for service, outcome in (expected.get("dependency_gates") or {}).items():
        wait = waits.get(service)
        if wait is None:
            failures.append(
                f"no dependency gate was recorded for {service!r}. The fixture "
                f"declares services.{service}.wait_for, so either the gate was "
                "dropped from the config or it never ran."
            )
        elif wait.get("outcome") != outcome:
            failures.append(
                f"the {service!r} gate reports outcome "
                f"{wait.get('outcome')!r}, expected {outcome!r} "
                f"(skip_reason: {wait.get('skip_reason')}). A skipped gate "
                "means the target raced this dependency, which is the thing "
                "this fixture exists to prove does not happen."
            )
    return failures


def _check_contracts(report: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    measured = {contract["id"]: contract for contract in report.get("contracts") or []}
    failures = []
    for contract_id, want in (expected.get("contracts") or {}).items():
        got = measured.get(contract_id)
        if got is None:
            failures.append(f"{contract_id} is missing from the report entirely.")
            continue
        # Status and branch are checked together and reported together: a
        # contract that keeps its status while changing its branch reached the
        # same verdict for a different reason, and that is the change most
        # worth seeing.
        if got.get("status") == want["status"] and got.get("branch") == want["branch"]:
            continue
        failures.append(
            f"{contract_id} is {got.get('status')}/{got.get('branch')}, "
            f"expected {want['status']}/{want['branch']}: {got.get('summary')}"
        )
    return failures


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
