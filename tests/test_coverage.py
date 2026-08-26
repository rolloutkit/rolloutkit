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

The last section closes the gap in all of the above: everything so far reads
`fixtures/matrix.yaml`, so a branch claim written straight into a Python test
was invisible here. That is not hypothetical. When `readiness-fallback-fast`
was deleted from the matrix, a hand-written copy of the same claim stayed
behind in `tests/test_fixtures.py` and kept betting on which side of a
host-dependent comparison the run would land — for one machine-day, until a
quieter machine landed on the other side. The matrix had been cleaned; the
claim had not, and nothing was looking at it.
"""

from __future__ import annotations

import ast
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
TESTS = Path(__file__).resolve().parent

#: The `decision_unit` classification as it was reviewed, spelled out here and
#: not derived from anything. The catalog says which branches claim the type;
#: this says which ones are allowed to. They have to agree.
#:
#: A list of exceptions grows quietly, one plausible entry at a time, until the
#: guarantee it qualifies is gone. This is the same list read the other way: it
#: fails the moment the two disagree, so moving a branch off live-image proof is
#: a change someone has to make here, on purpose, and defend in review.
#: SP005.readiness_fallback_below_resolution was in this set until the fallback
#: rule gained an absolute window floor. It was here because the ratio compares
#: the service against the host, so a live fixture proved nothing the host had
#: not already decided. The floor is not a comparison against the host, and a
#: readiness endpoint with no work behind it is under it on every machine tried,
#: so the branch went back to live-image proof and the row came back with it.
REVIEWED_DECISION_UNIT = {
    ("SP004", "budget_below_teardown_floor"),
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


def _covered_branch_names() -> set[str]:
    return {branch for _, branch in _covered()}


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


# --- Branch claims written into Python tests -------------------------------
#
# Everything above compares the contracts against `fixtures/matrix.yaml`. A test
# that names a branch in Python is making the same kind of claim in a place the
# comparison never looks. The rest of this file finds those claims and holds
# them to the same registry.


def _branch_names() -> set[str]:
    """Branch identifiers as the catalog spells them.

    Read from the catalog on purpose. A hand-written list here would need
    updating whenever a branch is added, and the update that gets forgotten is
    the one for the branch nobody is watching — which is the whole failure.
    """
    return {verdict.branch for doc in CATALOG.values() for verdict in doc.verdicts}


def _proof_files() -> dict[str, set[str]]:
    """For each decision_unit branch, the test files allowed to name it."""
    files: dict[str, set[str]] = {}
    for (_, branch), proof in _decision_unit().items():
        files.setdefault(branch, set()).add(proof.split("::")[0])
    return files


def _is_branch_expression(node: ast.expr) -> bool:
    """Whether this expression reads the branch off a verdict.

    The spellings the reports are read in: `result.branch` on a
    `ContractResult`, and `contract["branch"]` or `contract.get("branch")` on a
    parsed JSON document.
    """
    if isinstance(node, ast.Attribute):
        return node.attr == "branch"
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "branch"
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) >= 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "branch"
        )
    return False


def _strings(node: ast.expr, bound: dict[str, frozenset[str]]) -> set[str]:
    """The string literals an expression can supply, following module constants.

    Following the constants matters: `sp005["branch"] in COUNT_DEPENDENT` is the
    same claim as writing the two names at the comparison, and a check that only
    read literals in place would be avoided by accident the first time someone
    tidied a test into a module-level set.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Set | ast.Tuple | ast.List):
        return {name for element in node.elts for name in _strings(element, bound)}
    if isinstance(node, ast.Name):
        return set(bound.get(node.id, frozenset()))
    return set()


def _module_constants(tree: ast.Module) -> dict[str, frozenset[str]]:
    return {
        statement.targets[0].id: frozenset(_strings(statement.value, {}))
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and _strings(statement.value, {})
    }


def _claims(path: Path) -> list[tuple[int, str]]:
    """Every branch a test file states a verdict took, with its line number.

    A claim is a comparison with a branch expression on one side and a branch
    identifier on the other. Nothing looser: the identifiers double as evidence
    keys and appear in prose, so `environment["port_proxy_likely"]` and a
    docstring mentioning `budget_below_teardown_floor` are not claims and must
    not be reported as if they were. A check that cried wolf on those would be
    silenced within the week.
    """
    tree = ast.parse(path.read_text())
    bound = _module_constants(tree)
    names = _branch_names()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        claimed: set[str] = set()
        for index, side in enumerate(sides):
            others = [other for position, other in enumerate(sides) if position != index]
            if _is_branch_expression(side):
                for other in others:
                    claimed |= _strings(other, bound)
            elif isinstance(side, ast.Tuple):
                # `(result.status, result.branch) == (Status.FAIL, "killed")`
                for other in others:
                    if not isinstance(other, ast.Tuple) or len(other.elts) != len(side.elts):
                        continue
                    for mine, theirs in zip(side.elts, other.elts, strict=True):
                        if _is_branch_expression(mine):
                            claimed |= _strings(theirs, bound)
        found.extend((node.lineno, name) for name in sorted(claimed & names))
    return found


def test_no_test_names_a_branch_the_registry_cannot_see() -> None:
    """A branch identifier in a test is a coverage claim. It has to be registered.

    Two registries exist, and every claim belongs to one of them. A live_image
    branch is registered by its row in `fixtures/matrix.yaml`, which the tests
    above check against the contracts. A decision_unit branch is registered by
    the proof the catalog names, and only the file holding that proof may name
    it — a live test that names one is asserting an outcome its own
    classification says the image does not decide, which is a coin toss with a
    test wrapped around it.

    Matching is by branch name rather than by contract-and-branch, because the
    claims mostly do not say which contract they read. It costs nothing: every
    live_image branch already needs a matrix row to get past
    `test_every_branch_is_covered_by_a_runtime_fixture`, so the only names
    missing from the matrix are the decision_unit ones, and those are matched by
    file.
    """
    registered = _covered_branch_names()
    proofs = _proof_files()
    unregistered = [
        (path.relative_to(ROOT).as_posix(), line, branch)
        for path in sorted(TESTS.rglob("*.py"))
        if "__pycache__" not in path.parts
        for line, branch in _claims(path)
        if branch not in registered
        and path.relative_to(ROOT).as_posix() not in proofs.get(branch, set())
    ]
    assert not unregistered, (
        "these tests name a verdict branch that nothing registers:\n"
        + "\n".join(f"  {path}:{line}: {branch}" for path, line, branch in unregistered)
        + "\nRegister it or stop claiming it. A live_image branch is registered "
        "by a row in fixtures/matrix.yaml. A decision_unit branch is registered "
        "by the proof named in the catalog, and only that file may name it — if "
        "the claim is in a test that starts a container, the container is not "
        "what decides it. To read a branch name without claiming one, take it "
        "from the catalog, which is the registry itself."
    )


def test_the_claim_check_can_still_see_a_claim(tmp_path: Path) -> None:
    """The detector is precise by design, so its silence has to mean something.

    `_claims` ignores prose and evidence keys deliberately. That same precision
    is how it would fail open — one refactor into a spelling it does not read,
    and it reports nothing while everything looks green. This pins the spellings
    the test suite actually uses.
    """
    # Sorted, not arbitrary: a probe that picks a different branch on every
    # interpreter is a test whose failure cannot be reproduced.
    branch = sorted(_branch_names())[0]
    source = (
        f"WATCHED = {{'{branch}'}}\n"
        f"assert result.branch == '{branch}'\n"
        f"assert document['branch'] != '{branch}'\n"
        f"assert (result.status, result.branch) == (0, '{branch}')\n"
        f"assert document['branch'] in WATCHED\n"
        f"assert document.get('branch') == '{branch}'\n"
        f"assert environment['{branch}'] is False  # an evidence key, not a claim\n"
        f"NOISE = '{branch} appears in prose'\n"
    )
    path = tmp_path / "claim_probe.py"
    path.write_text(source)

    lines = [line for line, name in _claims(path) if name == branch]

    assert lines == [2, 3, 4, 5, 6], (
        f"_claims no longer reads every spelling a claim is written in: {lines}"
    )
