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
    first_step: str = ""


CATALOG: dict[str, ContractDoc] = {
    "SP001": ContractDoc(
        "SP001",
        "Starts the target, observes the first TCP accept, then polls readiness until the configured status appears; the readiness timestamp is compared with the startup budget and measured polling/start overhead resolution.",
        (
            "The target must start and readiness must return the expected status; otherwise the lifecycle experiment cannot reach a steady state and exits as an infrastructure error, not a misleading contract verdict.",
        ),
        (
            (
                "PASS",
                "Readiness is within the startup budget or the overrun is below measurement resolution.",
            ),
            ("WARN", "Readiness exceeds the startup budget by a resolvable amount."),
        ),
        "A slow readiness gate delays every rollout; a readiness endpoint that never passes leaves new replicas unavailable and can stall deployment progress.",
        "prestop, in_app, none",
        first_step="Inspect the target startup logs and compare readiness_ready_ms with the configured startup budget.",
    ),
    "SP002": ContractDoc(
        "SP002",
        "Sends ten sequential readiness probes after startup, compares their outcome signatures, maximum latency, and—when health is configured—the stable response body/headers/status against health.",
        ("SP001 must first observe successful readiness so the ten samples describe steady state rather than startup transition.",),
        (
            (
                "PASS",
                "All ten responses are the expected status and stay within the latency budget.",
            ),
            (
                "WARN",
                "Readiness duplicates health exactly or exceeds its latency budget.",
            ),
            ("FAIL", "The ten outcomes flap, or all settle on a status/error different from the configured expected status."),
        ),
        "A rollout controller can route traffic safely only when readiness is both truthful and stable.",
        "prestop, in_app, none",
        first_step="Replay /ready repeatedly from inside the service network and inspect why its status or latency changes.",
    ),
    "SP003": ContractDoc(
        "SP003",
        "Reads PID 1 signal disposition before shutdown, sends SIGTERM, and combines readiness/listener/voluntary-exit reactions with Docker kill/die events to distinguish a handled stop from discarded SIGTERM or SIGKILL.",
        ("The target must reach shutdown and Docker must report its exit; without an observed exit the experiment itself is incomplete.",),
        (
            ("PASS", "A voluntary shutdown is observed within the budget."),
            (
                "FAIL",
                "No shutdown reaction is observed, PID 1 discards SIGTERM, or SIGKILL is what ends the process.",
            ),
        ),
        "Containers whose PID 1 ignores or loses SIGTERM cannot perform graceful shutdown.",
        "prestop, in_app, none",
        first_step="Inspect pid1, runtime_handler_installed, and image entrypoint evidence; make the application process PID 1 and install/forward SIGTERM.",
    ),
    "SP004": ContractDoc(
        "SP004",
        "After T0 (SIGTERM), sends a fresh TCP/HTTP accept probe every 50ms, records the last accepted connection and resets, and compares that accept window with the declared drain strategy/window.",
        (
            "For in_app, shutdown must visibly start; otherwise there is no application-owned drain transition to measure.",
            "For in_app, traffic must reach the target directly; a host port proxy can accept on the application's behalf, so its listener timestamp is not evidence.",
            "Short shutdown budgets must exceed measured teardown resolution, and in_app_window must exceed 20 probe intervals; smaller boundaries cannot be distinguished reliably.",
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
        first_step="Check the strategy first: add a preStop sleep for platform-owned draining, or use --drain in_app and keep readiness/listener open for the declared window.",
    ),
    "SP005": ContractDoc(
        "SP005",
        "Measures a steady-state request baseline, launches concurrent requests, waits until their sockets are connected, sends SIGTERM while they are unfinished, then classifies every accepted request as complete, reset, truncated, or failed; completion must be 100%.",
        (
            "contracts.inflight must not be explicitly null; null is an intentional opt-out and therefore SKIP.",
            "When readiness is the zero-config fallback, its p50 must be at least 10x probe jitter so the before/after-SIGTERM boundary is distinguishable; a configured --inflight-path bypasses this fallback gate.",
            "Shutdown must visibly start, otherwise request outcomes cannot be attributed to a real shutdown transition.",
            "The pre-signal request baseline must be all 2xx, otherwise shutdown cannot be blamed for failures already present in steady state.",
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
        (
            "prestop: still requires every already accepted request to complete; the hook only prevents additional routing before T0.",
            "in_app: the application owns both stopping new work and completing work accepted before T0.",
            "none: completion is still measured, but there is no separate routing-gap protection for new work.",
        ),
        first_step="Inspect broken_requests and window evidence; if readiness fallback is inconclusive, point --inflight-path at a slower representative endpoint.",
    ),
    "SP006": ContractDoc(
        "SP006",
        "Measures SIGTERM-to-exit on Docker daemon kill/die timestamps when available, compares it with termination grace minus preStop duration, and treats an effective SIGKILL as deadline failure regardless of the final timestamp.",
        ("For budgets at or below 2s, the budget must exceed the sidecar-measured teardown resolution; otherwise daemon overhead is too large relative to the claimed deadline. Longer budgets skip calibration because the floor cannot affect the verdict.",),
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
        first_step="Compare shutdown_duration_ms and margin_ms, then profile the application's shutdown hooks and slow dependency cleanup within the remaining budget.",
    ),
}
