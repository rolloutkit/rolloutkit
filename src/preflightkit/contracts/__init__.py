from preflightkit.contracts.base import Contract, ContractResult, Status
from preflightkit.contracts.deadline import DeadlineContract
from preflightkit.contracts.drain import DrainWindowContract
from preflightkit.contracts.inflight import InflightContract
from preflightkit.contracts.readiness import ReadinessStabilityContract
from preflightkit.contracts.signals import SignalContract
from preflightkit.contracts.startup import StartupContract

#: Evaluation order is report order: startup, then the shutdown sequence.
ALL_CONTRACTS: tuple[Contract, ...] = (
    StartupContract(),
    ReadinessStabilityContract(),
    SignalContract(),
    DrainWindowContract(),
    InflightContract(),
    DeadlineContract(),
)

__all__ = ["ALL_CONTRACTS", "Contract", "ContractResult", "Status"]
