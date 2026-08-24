"""The matrix has to account for every verdict a contract can reach.

Pure data — no Docker, no container, no network. It compares what the contracts
declare against what `fixtures/matrix.yaml` claims, which means it runs on every
machine and in every pipeline, including the ones where the Docker-marked tests
are skipped.

The failure it exists to prevent is specific and has already happened once. SP006
declared a FAIL, the matrix asserted PASS on two fixtures, and the FAIL branch
was unreachable in the code — so a profile that blew its shutdown budget was
reported as a WARN and CI stayed green. No test failed, because no test was
looking at the branch. This one is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from preflightkit.contracts import ALL_CONTRACTS
from preflightkit.contracts.base import Status

MATRIX = Path(__file__).resolve().parent.parent / "fixtures" / "matrix.yaml"


def _matrix() -> dict:
    return yaml.safe_load(MATRIX.read_text())


def _declared() -> dict[tuple[str, str], Status]:
    return {
        (contract.id, branch): status
        for contract in ALL_CONTRACTS
        for branch, status in contract.BRANCHES.items()
    }


def _covered() -> set[tuple[str, str]]:
    return {
        (contract_id, expectation["branch"])
        for entry in _matrix()["fixtures"]
        for contract_id, expectation in entry["expect"].items()
    }


def _documented() -> set[tuple[str, str]]:
    return {(item["contract"], item["branch"]) for item in _matrix()["uncovered"]}


def _unit_covered() -> set[tuple[str, str]]:
    return {
        (item["contract"], item["branch"])
        for item in _matrix().get("unit_covered", [])
    }


def test_every_contract_declares_its_branches() -> None:
    for contract in ALL_CONTRACTS:
        assert isinstance(contract.required, bool), (
            f"{contract.id} must declare whether it is required"
        )
        branches = getattr(contract, "BRANCHES", None)
        assert branches, f"{contract.id} declares no BRANCHES"
        assert all(isinstance(s, Status) for s in branches.values())


def test_every_precondition_declares_its_engine_branch() -> None:
    for contract in ALL_CONTRACTS:
        for precondition in contract.PRECONDITIONS:
            assert contract.BRANCHES.get(precondition.branch) is precondition.unmet_status, (
                f"{contract.id}.{precondition.id} must declare branch "
                f"{precondition.branch} as {precondition.unmet_status}"
            )


def test_every_branch_is_covered_or_documented() -> None:
    """The rule itself: no verdict branch goes untested and unexplained."""
    declared = set(_declared())
    accounted = _covered() | _unit_covered() | _documented()
    missing = declared - accounted
    assert not missing, (
        "these verdict branches are neither covered by a fixture nor listed "
        f"under `uncovered` with a reason: {sorted(missing)}"
    )


def test_the_matrix_names_only_real_branches() -> None:
    """A typo in the matrix must fail loudly, not silently cover nothing."""
    declared = set(_declared())
    unknown = (_covered() | _unit_covered() | _documented()) - declared
    assert not unknown, f"matrix names branches no contract declares: {sorted(unknown)}"


def test_a_covered_branch_is_not_also_excused() -> None:
    """Otherwise a stale excuse survives the fixture that made it obsolete."""
    both = _covered() & _documented()
    assert not both, f"listed as unreachable but covered by a fixture: {sorted(both)}"


def test_every_excuse_gives_a_reason() -> None:
    for item in _matrix()["uncovered"]:
        reason = (item.get("reason") or "").strip()
        assert len(reason) > 40, (
            f"{item['contract']}.{item['branch']} needs a reason saying what "
            "makes the branch unreachable"
        )


def test_every_unit_covered_branch_names_an_existing_test() -> None:
    root = MATRIX.parent.parent
    for item in _matrix().get("unit_covered", []):
        path_text, separator, test_name = item["test"].partition("::")
        assert separator and test_name.startswith("test_")
        path = root / path_text
        assert path.is_file(), f"unit coverage file does not exist: {path_text}"
        assert f"def {test_name}(" in path.read_text(), (
            f"unit coverage test does not exist: {item['test']}"
        )


@pytest.mark.parametrize(
    "entry", _matrix()["fixtures"], ids=lambda e: e["name"]
)
def test_expected_status_matches_the_branch(entry: dict) -> None:
    """The matrix cannot expect a branch to produce a status it never produces."""
    declared = _declared()
    for contract_id, expectation in entry["expect"].items():
        key = (contract_id, expectation["branch"])
        assert declared[key] == expectation["status"], (
            f"{entry['name']}: {contract_id}.{expectation['branch']} yields "
            f"{declared[key]}, but the matrix expects {expectation['status']}"
        )


def test_every_fixture_config_exists() -> None:
    root = MATRIX.parent
    for entry in _matrix()["fixtures"]:
        assert (root / entry["config"]).is_file(), f"{entry['name']}: missing config"
    for image in _matrix()["images"]:
        context = root / image["context"]
        dockerfile = context / image.get("dockerfile", "Dockerfile")
        assert dockerfile.is_file(), f"{image['name']}: missing {dockerfile}"
