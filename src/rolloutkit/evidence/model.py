"""The complete result of one or more runs."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field, replace
from typing import Any

from rolloutkit.contracts.base import SEVERITY, ContractResult, Status
from rolloutkit.engine.context import RunReport


def new_run_id() -> str:
    raw = base64.b32encode(os.urandom(6)).decode().rstrip("=")
    return f"rk_{raw}"


@dataclass(slots=True)
class RunOutcome:
    """One execution: what was measured, and what the contracts made of it."""

    report: RunReport
    results: list[ContractResult] = field(default_factory=list)
    duration_ms: float = 0.0

    def by_id(self) -> dict[str, ContractResult]:
        return {r.id: r for r in self.results}


@dataclass(slots=True)
class Session:
    """A whole invocation: one run, or several when --repeat is used."""

    run_id: str
    image: str
    runs: list[RunOutcome] = field(default_factory=list)
    infrastructure_error: str | None = None

    @property
    def contract_ids(self) -> list[str]:
        seen: list[str] = []
        for run in self.runs:
            for result in run.results:
                if result.id not in seen:
                    seen.append(result.id)
        return seen

    def aggregate(self, contract_id: str) -> ContractResult:
        """Combine repeats.

        A contract whose verdict is not stable across runs becomes FLAKY, which
        never blocks CI — an unstable signal is a measurement problem, and
        blocking on it teaches people to disable the tool.
        """
        results = [r.by_id()[contract_id] for r in self.runs if contract_id in r.by_id()]
        inconclusive = [r for r in results if r.status is Status.INCONCLUSIVE]
        measured = [r for r in results if r.status is not Status.INCONCLUSIVE]
        if measured:
            results = measured
        statuses = {r.status for r in results}
        worst = max(results, key=lambda r: SEVERITY[r.status])
        if len(statuses) > 1:
            flaky = ContractResult(
                id=worst.id,
                name=worst.name,
                status=Status.FLAKY,
                summary=f"verdict differed across {len(results)} runs: "
                + ", ".join(sorted(str(s) for s in statuses)),
                # Deliberately branchless. FLAKY is not a code path any contract
                # can take; it is a statement about a set of runs that took
                # different ones. Carrying the worst run's branch here would tell
                # the matrix a lie it is designed to catch.
                branch="",
                expected=worst.expected,
                actual=worst.actual,
                evidence=worst.evidence,
                notes=worst.notes + ["FLAKY never blocks CI."],
                required=worst.required,
            )
            if inconclusive:
                flaky.notes.append(
                    f"{len(inconclusive)} repeat(s) were INCONCLUSIVE."
                )
            return flaky
        if inconclusive and measured:
            return replace(
                worst,
                notes=worst.notes
                + [
                    f"{len(inconclusive)} repeat(s) were INCONCLUSIVE; the verdict "
                    "uses only measurable repeats."
                ],
            )
        return worst

    @property
    def aggregated(self) -> list[ContractResult]:
        return [self.aggregate(cid) for cid in self.contract_ids]

    @property
    def worst_status(self) -> Status:
        if not self.runs:
            return Status.ERROR
        return max((r.status for r in self.aggregated), key=lambda s: SEVERITY[s])

    @property
    def verdict_status(self) -> Status:
        """Worst measured verdict, excluding configuration and measurability."""
        measured = [
            result.status
            for result in self.aggregated
            if result.status not in (Status.SKIP, Status.INCONCLUSIVE)
        ]
        return max(measured, key=lambda status: SEVERITY[status], default=Status.PASS)

    @property
    def inconclusive(self) -> list[ContractResult]:
        by_contract: dict[str, ContractResult] = {}
        for run in self.runs:
            for result in run.results:
                if result.status is Status.INCONCLUSIVE:
                    by_contract.setdefault(result.id, result)
        return list(by_contract.values())

    @property
    def required_unmeasured(self) -> list[ContractResult]:
        """Required contracts with any SKIP or INCONCLUSIVE run."""
        by_contract: dict[str, ContractResult] = {}
        for run in self.runs:
            for result in run.results:
                if result.required and result.status in (
                    Status.SKIP,
                    Status.INCONCLUSIVE,
                ):
                    by_contract.setdefault(result.id, result)
        return list(by_contract.values())

    def timing_spread(self, extract: Any) -> dict[str, float] | None:
        values = [v for v in (extract(r.report) for r in self.runs) if v is not None]
        if not values:
            return None
        values.sort()
        mid = len(values) // 2
        median = (
            values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        )
        return {"median": median, "min": values[0], "max": values[-1]}
