"""The one fixture whose image nobody here wrote, and the check that guards it.

`fixtures/oss/paperless-ngx/` is the acceptance run against a third-party
application: real image, real Postgres, real Redis, migrations at startup and a
shutdown sequence designed by somebody else. Everything else in `fixtures/`
is a container this repository builds, which means the suite could be entirely
green while the pipeline failed on the first application anybody pointed it at.

None of that can be checked without Docker, and these tests use none: they check
the two documents the CI job reads, and the script that compares them. The run
itself is the `oss-app` job's business. What is checked here is that the job
cannot pass for the wrong reason — an `expected.yaml` naming a contract that no
longer exists, an image that stopped being pinned, or a comparison that returns
0 no matter what it is handed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from rolloutkit.config.loader import load_config
from rolloutkit.contracts import ALL_CONTRACTS

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "oss" / "paperless-ngx"
CONFIG = FIXTURE / "rolloutkit.yaml"
EXPECTED = FIXTURE / "expected.yaml"
SCRIPT = ROOT / "scripts" / "assert_oss_run.py"


def _expected() -> dict[str, Any]:
    return yaml.safe_load(EXPECTED.read_text())


def test_the_fixture_config_is_one_this_release_can_load() -> None:
    """A config the loader rejects fails the job in a step that measures nothing.

    Worth its own test because this file is the only one in the repository that
    the fast suite would otherwise never open: every other fixture is read by
    the matrix, and this one is deliberately not in it.
    """
    config = load_config(config_path=CONFIG)

    assert set(config.services) == {"db", "broker"}
    assert config.services["db"].wait_for is not None
    assert config.services["broker"].wait_for is not None
    assert config.contracts.inflight is not None
    # Named rather than left to the readiness fallback, which needs the probe
    # path to be ten times the measurement jitter. `/accounts/login/` answers in
    # about 13ms, so on a quiet host it clears that and on a busy one it does
    # not — a fixture that decides by luck.
    assert config.contracts.inflight.request.path == "/accounts/login/"


def test_every_image_is_pinned_by_digest() -> None:
    """A tag moves on upstream's schedule; a digest moves in a commit.

    Without this, an upstream release turns into a red build nobody here caused,
    and the pinned verdicts in `expected.yaml` become claims about an image that
    no longer exists.
    """
    config = load_config(config_path=CONFIG)
    images = [config.target.image, *(s.image for s in config.services.values())]

    unpinned = [image for image in images if "@sha256:" not in image]

    assert not unpinned, (
        f"these images are not pinned by digest: {unpinned}. Upstream would "
        "decide when this job goes red."
    )


def test_expected_names_only_contracts_and_branches_that_exist() -> None:
    """The pins have to be about something.

    A contract renamed or a branch retired leaves `expected.yaml` asserting a
    key that is simply absent from every report — and the assertion script would
    report that as a failure of the run rather than of this file. This says so
    here, in the fast suite, where it is cheap.
    """
    declared = {contract.id: contract.BRANCHES for contract in ALL_CONTRACTS}
    expected = _expected()["contracts"]

    assert set(expected) == set(declared), (
        "expected.yaml pins a different set of contracts than the tool declares: "
        f"{sorted(set(expected) ^ set(declared))}"
    )
    for contract_id, pin in expected.items():
        assert pin["branch"] in declared[contract_id], (
            f"{contract_id} has no branch {pin['branch']!r}"
        )
        assert pin["status"] == declared[contract_id][pin["branch"]].value, (
            f"{contract_id}.{pin['branch']} is declared "
            f"{declared[contract_id][pin['branch']]}, not {pin['status']}"
        )


def test_expected_pins_a_gate_for_every_dependency_that_declares_one() -> None:
    """The gate is why this fixture is the one that proves `wait_for` matters.

    Paperless runs its migrations against Postgres at startup. If a gate stopped
    being asserted, the fixture would keep passing on a fast machine and start
    racing on a slow one, which is the failure `wait_for` exists to remove.
    """
    config = load_config(config_path=CONFIG)
    gated = {name for name, s in config.services.items() if s.wait_for is not None}

    assert set(_expected()["dependency_gates"]) == gated


def _check(report: dict[str, Any], tmp_path: Path, exit_code: int = 0) -> int:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(report))
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), str(EXPECTED), str(exit_code)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


def _matching_report() -> dict[str, Any]:
    expected = _expected()
    return {
        "result": expected["result"],
        "environment": {"probe_location": expected["probe_location"]},
        "dependency_waits": [
            {"service": name, "outcome": outcome}
            for name, outcome in expected["dependency_gates"].items()
        ],
        "contracts": [
            {"id": contract_id, "summary": "", **pin}
            for contract_id, pin in expected["contracts"].items()
        ],
    }


def test_a_matching_report_passes(tmp_path: Path) -> None:
    assert _check(_matching_report(), tmp_path) == 0


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(
            lambda r: r.update(result="PASS"),
            id="the overall result moved",
        ),
        pytest.param(
            lambda r: r["environment"].update(probe_location="host_fallback"),
            id="the measurement came from the host",
        ),
        pytest.param(
            lambda r: r["dependency_waits"][0].update(outcome="skipped"),
            id="a dependency gate was skipped",
        ),
        pytest.param(
            lambda r: r["dependency_waits"].clear(),
            id="no gate ran at all",
        ),
        pytest.param(
            lambda r: r["contracts"][0].update(status="FAIL"),
            id="a contract changed its status",
        ),
        pytest.param(
            lambda r: r["contracts"][0].update(branch="over_budget"),
            id="a contract reached the same status by another route",
        ),
        pytest.param(
            lambda r: r["contracts"].pop(0),
            id="a contract is missing from the report",
        ),
    ],
)
def test_a_report_that_drifted_fails(break_it, tmp_path: Path) -> None:
    """Every direction, because a guard that cannot fail reports its own silence.

    The status and branch cases are separate on purpose: a contract that keeps
    its status while changing its branch reached the same verdict for a
    different reason, and that is the change most worth seeing.
    """
    report = _matching_report()
    break_it(report)

    assert _check(report, tmp_path) == 1


def test_the_exit_code_is_checked_and_lives_nowhere_in_the_report(
    tmp_path: Path,
) -> None:
    """The one pin the JSON cannot carry.

    `result` is what the contracts added up to; the exit code is the --fail-on
    policy applied to that sum. A change to the default policy would leave every
    field in the report untouched and silently start — or stop — failing every
    pipeline that shells out to this tool.
    """
    assert _check(_matching_report(), tmp_path, exit_code=1) == 1
