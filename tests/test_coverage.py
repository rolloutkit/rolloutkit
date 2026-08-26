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

That guarantee protects the tool's reading of reality, which is why it asks for
a real image. Not every branch reads reality. A branch classified
`Evidence.DECISION_UNIT` in the catalog is decided by comparing numbers already
measured, with the image contributing nothing to which side it lands on; running
a container for it proves no more than the arithmetic states, and feeds the
comparison whatever the host happened to produce, so the verdict tracks the
machine instead of the target. Those branches are proved by a named test that
hands the decision function known values, and the gate below runs it.

The classification is deliberately awkward to extend: the catalog declares the
type, `REVIEWED_DECISION_UNIT` below repeats it independently, and the two must
agree. That is the difference between a rule and an exception list — an
exception list absorbs a new entry in silence, this fails until someone states
the case for the entry in both places.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from preflightkit.contracts import ALL_CONTRACTS
from preflightkit.contracts.base import Status
from preflightkit.contracts.catalog import CATALOG, Evidence

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "fixtures" / "matrix.yaml"

#: The `decision_unit` classification as it was reviewed, spelled out here and
#: not derived from anything. The catalog says which branches claim the type;
#: this says which ones are allowed to. They have to agree.
#:
#: A list of exceptions grows quietly, one plausible entry at a time, until the
#: guarantee it qualifies is gone. This is the same list read the other way: it
#: fails the moment the two disagree, so moving a branch off live-image proof is
#: a change someone has to make here, on purpose, and defend in review.
REVIEWED_DECISION_UNIT = {
    ("SP004", "budget_below_teardown_floor"),
    ("SP005", "readiness_fallback_below_resolution"),
    ("SP006", "budget_below_teardown_floor"),
}


def _matrix() -> dict:
    return yaml.safe_load(MATRIX.read_text())


def _decision_unit() -> dict[tuple[str, str], str]:
    """Branches the catalog classifies as decided by arithmetic, and their proof."""
    return {
        (doc.id, verdict.branch): verdict.proof
        for doc in CATALOG.values()
        for verdict in doc.verdicts
        if verdict.evidence is Evidence.DECISION_UNIT
    }


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
    """Every live_image branch needs a real image. Prose cannot stand in for one."""
    declared = set(_declared())
    missing = declared - _covered() - set(_decision_unit())
    assert not missing, (
        "these verdict branches have no runtime fixture: "
        + ", ".join(f"{contract}.{branch}" for contract, branch in sorted(missing))
        + ". Add a row to fixtures/matrix.yaml. A branch is exempt only by being "
        "classified Evidence.DECISION_UNIT in the catalog, which requires that "
        "its verdict be decided by comparing already-measured numbers, with the "
        "image contributing nothing to which side it lands on."
    )


def test_the_decision_unit_classification_is_the_one_reviewed() -> None:
    """The classification is a rule, not a growing list of exceptions."""
    claimed = set(_decision_unit())
    assert claimed == REVIEWED_DECISION_UNIT, (
        "the catalog's decision_unit branches are not the reviewed set.\n"
        f"  added, not reviewed: {sorted(claimed - REVIEWED_DECISION_UNIT)}\n"
        f"  reviewed, now gone:  {sorted(REVIEWED_DECISION_UNIT - claimed)}\n"
        "Classifying a branch decision_unit drops its live-image coverage, so it "
        "is not a local edit to the catalog. Show that the branch is decided by "
        "arithmetic over values already measured — that the image under test is "
        "not an input and a live fixture would only supply the comparison with "
        "whatever the host produced — then update REVIEWED_DECISION_UNIT here."
    )


def test_a_decision_unit_branch_does_not_also_claim_a_live_fixture() -> None:
    """Two kinds of proof for one branch means neither one is load-bearing."""
    both = set(_decision_unit()) & _covered()
    assert not both, (
        "these branches are classified decision_unit but still have a matrix row: "
        + ", ".join(f"{contract}.{branch}" for contract, branch in sorted(both))
        + ". The row is the coin toss the classification exists to remove; drop "
        "it, or drop the classification."
    )


def test_only_decision_unit_branches_name_a_proof() -> None:
    for doc in CATALOG.values():
        for verdict in doc.verdicts:
            if verdict.evidence is Evidence.DECISION_UNIT:
                assert verdict.proof, (
                    f"{doc.id}.{verdict.branch} is decision_unit but names no "
                    "proof; give it the pytest node id that feeds the decision "
                    "function known values"
                )
            else:
                assert not verdict.proof, (
                    f"{doc.id}.{verdict.branch} names a proof but is proved by a "
                    "live image; the node id would not be run"
                )


@pytest.mark.parametrize(
    ("key", "proof"),
    sorted(_decision_unit().items()),
    ids=lambda v: v if isinstance(v, str) else ".".join(v),
)
def test_every_decision_unit_branch_names_a_proof_that_passes(
    key: tuple[str, str], proof: str
) -> None:
    """Run the named test. A branch is only proved while its proof is green."""
    path = proof.split("::")[0]
    assert (ROOT / path).is_file(), f"{key}: no such test file: {path}"
    assert Path(path).name != Path(__file__).name, (
        f"{key}: a branch cannot be proved by this file — the gate would run "
        "itself"
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", proof, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, (
        f"{key[0]}.{key[1]} is covered only by {proof}, and it did not pass. "
        "While it is red the branch has no proof of any kind — no fixture "
        "exercises it either.\n" + run.stdout[-2000:]
    )
    assert " 1 passed" in run.stdout or "\n1 passed" in run.stdout, (
        f"{key[0]}.{key[1]}: {proof} selected no test — it was probably renamed. "
        f"pytest said: {run.stdout.strip()[-400:]}"
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
