"""`explain` has to describe every verdict a contract can actually reach.

Pure data, like `tests/test_coverage.py`, and the same argument applied to
documentation instead of fixtures. A report names a branch — `SP004 FAIL /
in_app_listener_closed_early` — and `explain` is where that name is looked up
offline. A branch nobody documented is a verdict the reader cannot resolve, and
a documented branch with the wrong status is worse than none at all: it is
confidently wrong.
"""

from __future__ import annotations

from preflightkit.config.models import DrainStrategy
from preflightkit.contracts import ALL_CONTRACTS
from preflightkit.contracts.catalog import ANY_STRATEGY, CATALOG


def test_the_catalog_documents_every_contract() -> None:
    assert set(CATALOG) == {contract.id for contract in ALL_CONTRACTS}


def test_every_branch_is_documented() -> None:
    declared = {
        (contract.id, branch)
        for contract in ALL_CONTRACTS
        for branch in contract.BRANCHES
    }
    documented = {
        (doc.id, verdict.branch) for doc in CATALOG.values() for verdict in doc.verdicts
    }
    missing = declared - documented
    assert not missing, (
        "explain documents no verdict for: "
        + ", ".join(f"{contract}.{branch}" for contract, branch in sorted(missing))
    )
    phantom = documented - declared
    assert not phantom, (
        "explain documents branches no contract reaches: "
        + ", ".join(f"{contract}.{branch}" for contract, branch in sorted(phantom))
    )


def test_documented_status_matches_the_branch() -> None:
    for contract in ALL_CONTRACTS:
        doc = CATALOG[contract.id]
        for verdict in doc.verdicts:
            declared = contract.BRANCHES[verdict.branch]
            assert verdict.status == declared.value, (
                f"{contract.id}.{verdict.branch} yields {declared.value}, but "
                f"explain documents it as {verdict.status}"
            )


def test_every_branch_is_documented_once() -> None:
    for doc in CATALOG.values():
        branches = [verdict.branch for verdict in doc.verdicts]
        assert len(branches) == len(set(branches)), f"{doc.id} repeats a branch"


def test_strategy_applicability_names_only_real_strategies() -> None:
    strategies = {strategy.value for strategy in DrainStrategy}
    assert set(ANY_STRATEGY) == strategies
    for doc in CATALOG.values():
        for verdict in doc.verdicts:
            assert verdict.applies_to, (
                f"{doc.id}.{verdict.branch} is reachable under no strategy"
            )
            assert set(verdict.applies_to) <= strategies, (
                f"{doc.id}.{verdict.branch} names an unknown strategy"
            )


def test_sp004_partitions_its_verdicts_by_strategy() -> None:
    """The contract most often misread: its verdicts are strategy-specific.

    `prestop` and `none` each short-circuit to exactly one outcome. Documenting
    them in one undifferentiated table is what makes a reader expect a listener
    verdict that the code can never produce for their configuration.
    """
    doc = CATALOG["SP004"]
    assert doc.strategy_dependent
    reachable = {
        strategy: [v.branch for v in doc.verdicts if strategy in v.applies_to]
        for strategy in ANY_STRATEGY
    }
    assert reachable["prestop"] == ["prestop_not_applicable", "budget_below_teardown_floor"]
    assert reachable["none"] == ["none_uncovered", "budget_below_teardown_floor"]
    assert "in_app_listener_closed_early" in reachable["in_app"]


def test_contracts_whose_verdicts_ignore_the_strategy_say_so() -> None:
    for doc in CATALOG.values():
        if doc.id == "SP004":
            continue
        assert not doc.strategy_dependent, (
            f"{doc.id} documents a strategy-specific verdict; explain would "
            "render it grouped, so the grouping needs a test of its own"
        )


def test_every_contract_answers_the_five_questions() -> None:
    """What is measured, which preconditions, verdicts, why, first step."""
    for contract in ALL_CONTRACTS:
        doc = CATALOG[contract.id]
        assert doc.measures.strip(), f"{doc.id} does not say what it measures"
        assert doc.preconditions, f"{doc.id} lists no preconditions"
        assert doc.verdicts, f"{doc.id} lists no verdicts"
        assert doc.why.strip(), f"{doc.id} does not say why it matters"
        assert doc.first_step.strip(), f"{doc.id} gives no first step after FAIL"
        # Prose is checked by count, not by wording: a precondition added to the
        # engine without a line here is the failure worth catching.
        assert len(doc.preconditions) >= len(contract.PRECONDITIONS), (
            f"{doc.id} declares {len(contract.PRECONDITIONS)} preconditions but "
            f"documents {len(doc.preconditions)}"
        )
