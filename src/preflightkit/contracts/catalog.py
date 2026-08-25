"""Static, offline documentation for the public contract catalogue."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContractDoc:
    id: str
    measures: str
    preconditions: tuple[str, ...]
    verdicts: tuple[tuple[str, str], ...]
    why: str
    strategies: str
    strategy_notes: tuple[str, ...] = ()


CATALOG: dict[str, ContractDoc] = {
    "SP001": ContractDoc(
        "SP001",
        "Time from container start to TCP availability and successful readiness.",
        (
            "The container starts and its readiness endpoint eventually returns the expected status.",
        ),
        (
            (
                "PASS",
                "Readiness is within the startup budget or the overrun is below measurement resolution.",
            ),
            ("WARN", "Readiness exceeds the startup budget by a resolvable amount."),
            ("ERROR", "No successful readiness observation exists."),
        ),
        "Slow or missing readiness stalls a rollout before shutdown behavior can be tested.",
        "prestop, in_app, none",
    ),
    "SP002": ContractDoc(
        "SP002",
        "Correctness, stability, distinctness, and latency of ten steady-state readiness responses.",
        ("SP001 has first observed the configured expected readiness status.",),
        (
            (
                "PASS",
                "All ten responses are the expected status and stay within the latency budget.",
            ),
            (
                "WARN",
                "Readiness duplicates health exactly or exceeds its latency budget.",
            ),
            (
                "FAIL",
                "The ten responses flap, or become consistently different from the expected status after startup.",
            ),
            ("ERROR", "The readiness baseline could not be collected completely."),
        ),
        "A rollout controller can route traffic safely only when readiness is both truthful and stable.",
        "prestop, in_app, none",
    ),
    "SP003": ContractDoc(
        "SP003",
        "Whether PID 1 reacts to the stop signal and exits voluntarily before the deadline.",
        ("A stop signal is sent and the runtime remains observable through shutdown.",),
        (
            ("PASS", "A voluntary shutdown is observed within the budget."),
            (
                "FAIL",
                "The signal is discarded, SIGKILL is effective, or shutdown exceeds the deadline.",
            ),
        ),
        "Containers whose PID 1 ignores or loses SIGTERM cannot perform graceful shutdown.",
        "prestop, in_app, none",
    ),
    "SP004": ContractDoc(
        "SP004",
        "The gap between routing removal and the process stopping acceptance of new connections.",
        (
            "Shutdown starts.",
            "Listener timing is directly observable when the selected strategy needs it.",
            "The configured window is above host measurement resolution.",
        ),
        (
            (
                "PASS",
                "prestop owns the routing gap, or an in_app listener covers its declared window.",
            ),
            (
                "WARN",
                "drain is none, readiness never drops, or an in_app window has only a thin reserve.",
            ),
            (
                "FAIL",
                "New connections are reset or the in_app listener closes before its window ends.",
            ),
            ("INCONCLUSIVE", "A required timing precondition cannot be resolved."),
        ),
        "A clean process exit does not prevent rollout errors if new traffic can still arrive during shutdown.",
        "prestop, in_app, none",
        (
            "prestop: the platform hook owns routing removal before SIGTERM; listener probes stop at T0.",
            "in_app: the application must stay ready and accept for in_app_window after SIGTERM, then drain.",
            "none: no mechanism covers routing propagation; SP004 warns because connection loss is expected.",
        ),
    ),
    "SP005": ContractDoc(
        "SP005",
        "Whether every request already in flight at SIGTERM completes without reset or truncation.",
        (
            "SP005 is not explicitly disabled with contracts.inflight: null.",
            "Readiness fallback is at least 10x slower than daemon jitter, or --inflight-path is passed.",
            "Shutdown starts.",
            "The steady-state request baseline is 2xx.",
        ),
        (
            ("PASS", "Every request in flight at SIGTERM completes."),
            ("FAIL", "One or more in-flight requests are destroyed."),
            ("SKIP", "The user explicitly sets contracts.inflight to null."),
            (
                "INCONCLUSIVE",
                "Readiness is too fast relative to jitter, or shutdown/baseline cannot establish a valid window.",
            ),
            (
                "ERROR",
                "The experiment sends SIGTERM with no request actually in flight.",
            ),
        ),
        "This is the primary graceful-shutdown contract: accepted work must not be lost.",
        "prestop, in_app, none",
    ),
    "SP006": ContractDoc(
        "SP006",
        "Shutdown duration against grace minus preStop time, including whether SIGKILL was required.",
        ("The shutdown budget is larger than the host teardown measurement floor.",),
        (
            ("PASS", "The process exits voluntarily with comfortable margin."),
            ("WARN", "It exits voluntarily with less than 20 percent margin."),
            ("FAIL", "It overruns the deadline, never exits, or is ended by SIGKILL."),
            (
                "INCONCLUSIVE",
                "The configured budget is below measured teardown resolution.",
            ),
        ),
        "Platforms sever remaining work when the grace period expires.",
        "prestop, in_app, none",
    ),
}
