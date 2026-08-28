from rolloutkit.contracts.base import Contract, ContractResult, Status
from rolloutkit.contracts.deadline import DeadlineContract
from rolloutkit.contracts.drain import DrainWindowContract
from rolloutkit.contracts.inflight import InflightContract
from rolloutkit.contracts.readiness import ReadinessStabilityContract
from rolloutkit.contracts.signals import SignalContract
from rolloutkit.contracts.startup import StartupContract

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
