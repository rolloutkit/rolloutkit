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


def test_every_branch_is_covered_by_a_runtime_fixture() -> None:
    """No unit-only or prose exception can stand in for a runtime branch."""
    declared = set(_declared())
    missing = declared - _covered()
    assert not missing, (
        "these verdict branches have no runtime fixture: "
        + ", ".join(f"{contract}.{branch}" for contract, branch in sorted(missing))
    )


def test_the_matrix_names_only_real_branches() -> None:
    """A typo in the matrix must fail loudly, not silently cover nothing."""
    declared = set(_declared())
    unknown = _covered() - declared
    assert not unknown, f"matrix names branches no contract declares: {sorted(unknown)}"


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
