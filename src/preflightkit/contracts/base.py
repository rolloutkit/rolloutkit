"""Contract protocol.

Contracts are deliberately synchronous and pure: they receive a finished
`RunReport` and return a verdict. A contract that could perform I/O could
influence the very behaviour it is measuring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from preflightkit.engine.context import RunReport


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"
    FLAKY = "FLAKY"


#: Ranked worst-first, for aggregating repeated runs and picking an exit code.
SEVERITY = {
    Status.ERROR: 5,
    Status.FAIL: 4,
    Status.FLAKY: 3,
    Status.WARN: 2,
    Status.INCONCLUSIVE: 1,
    Status.SKIP: 1,
    Status.PASS: 0,
}


@dataclass(slots=True)
class ContractResult:
    """One verdict, plus the identity of the code path that produced it.

    `branch` exists so the fixture matrix can assert *why* a contract reached a
    verdict, not just that it did. Two different defects that both produce FAIL
    are not the same regression, and a matrix that cannot tell them apart will
    happily go green while a branch quietly stops working.
    """

    id: str
    name: str
    status: Status
    summary: str
    branch: str = ""
    expected: str = ""
    actual: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    required: bool = False


@dataclass(frozen=True, slots=True)
class Precondition:
    """A named fact the engine must resolve before a contract can run."""

    id: str
    branch: str
    unmet_status: Status = Status.INCONCLUSIVE


INFLIGHT_ENABLED = Precondition(
    "inflight_enabled", "disabled", unmet_status=Status.SKIP
)
READINESS_FALLBACK_RESOLVABLE = Precondition(
    "readiness_fallback_resolvable",
    "readiness_fallback_below_resolution",
    unmet_status=Status.INCONCLUSIVE,
)
SHUTDOWN_STARTED = Precondition("shutdown_started", "shutdown_never_started")
BASELINE_STEADY_STATE_2XX = Precondition(
    "baseline_steady_state_2xx", "baseline_not_2xx"
)
SHUTDOWN_BUDGET_RESOLVABLE = Precondition(
    "shutdown_budget_resolvable", "budget_below_teardown_floor"
)
DIRECT_CONNECTION_PATH = Precondition(
    "direct_connection_path", "port_proxy_likely"
)


class Contract(Protocol):
    id: str
    name: str
    required: bool
    #: Every verdict this contract can reach: branch id -> the status it yields.
    #: `tests/test_coverage.py` requires each one to be produced by a fixture or
    #: to be listed as unreachable, with a reason, in `fixtures/matrix.yaml`.
    BRANCHES: dict[str, Status]
    PRECONDITIONS: tuple[Precondition, ...]

    def evaluate(self, report: RunReport) -> ContractResult: ...
