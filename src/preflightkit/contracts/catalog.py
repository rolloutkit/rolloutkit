"""Static, offline documentation for the public contract catalogue.

A report names a `branch`, not just a status: `SP004 FAIL /
in_app_listener_closed_early`. The branch is the identity of the code path that
produced the verdict, so it is the thing a reader has to look up — and `explain`
is the only place they can look it up without a network connection. Every branch
a contract can reach therefore has a row here, and `tests/test_coverage.py`
fails if one is missing, renamed, or documented with the wrong status.

Where a verdict depends on the declared drain strategy, the row says which
strategy can reach it. SP004 is the contract that needs this: under `prestop` it
has exactly one outcome, under `none` exactly one, and the five listener
verdicts belong to `in_app` alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Rendered when a verdict is reachable under every declared drain strategy.
ANY_STRATEGY = ("prestop", "in_app", "none")


class Evidence(StrEnum):
    """What kind of proof a branch's coverage requires.

    The distinction is not about how important a branch is, it is about what its
    decision actually reads.

    `live_image` branches assert something about the target: whether the process
    exited, whether a connection was reset, whether readiness changed. The tool
    is reading reality, and only a real image can show that it read it correctly.
    That is the failure `tests/test_coverage.py` was built for — SP006 once
    declared a FAIL no code could reach, and CI stayed green because no fixture
    was looking.

    `decision_unit` branches assert something about the measurement instead: two
    numbers that are already in hand get compared, and the branch says whether
    the comparison can be resolved at all. The image under test is not an input.
    Running one proves nothing the arithmetic does not already state, and it
    supplies the comparison with whatever the host happened to produce — so the
    verdict varies by machine rather than by target. Those branches are proved by
    handing known values to the decision function, in a named test the gate runs.
    """

    LIVE_IMAGE = "live_image"
    DECISION_UNIT = "decision_unit"


@dataclass(frozen=True, slots=True)
class Verdict:
    """One reachable branch, its status, and what it means.

    `applies_to` is the set of drain strategies that can reach this branch. It
    is the full set for contracts whose verdict does not depend on the strategy.
    """

    branch: str
    status: str
    meaning: str
    applies_to: tuple[str, ...] = ANY_STRATEGY
    #: What kind of proof this branch's coverage requires. See `Evidence`.
    evidence: Evidence = Evidence.LIVE_IMAGE
    #: For `decision_unit` branches only: the pytest node id that proves this
    #: branch by feeding known values to the decision function. The gate in
    #: `tests/test_coverage.py` runs it, so a rename or a regression fails there
    #: rather than leaving the branch quietly unproved.
    proof: str = ""


@dataclass(frozen=True, slots=True)
class ContractDoc:
    id: str
    measures: str
    preconditions: tuple[str, ...]
    verdicts: tuple[Verdict, ...]
    why: str
    strategies: str
    strategy_notes: tuple[str, ...] = ()
    first_step: str = ""

    @property
    def strategy_dependent(self) -> bool:
        """True when at least one verdict is unreachable under some strategy."""
        return any(verdict.applies_to != ANY_STRATEGY for verdict in self.verdicts)


CATALOG: dict[str, ContractDoc] = {
    "SP001": ContractDoc(
        "SP001",
        "Starts the target, observes the first TCP accept, then polls readiness "
        "until the configured status appears. The readiness timestamp is compared "
        "with contracts.startup.budget, and the comparison is resolved against "
        "startup_resolution_ms — the Docker create/start/inspect overhead measured "
        "in the same run — so an overrun smaller than the instrument is not "
        "reported as an overrun.",
        (
            "The target must start and readiness must return the expected status; "
            "otherwise the lifecycle experiment cannot reach a steady state and "
            "exits as an infrastructure error, not a misleading contract verdict.",
        ),
        (
            Verdict(
                "within_budget",
                "PASS",
                "Readiness returned the configured status inside the startup budget.",
            ),
            Verdict(
                "within_resolution",
                "PASS",
                "Readiness passed after the budget, but by less than the run's "
                "measured startup resolution, so the budget was not provably missed.",
            ),
            Verdict(
                "over_budget",
                "WARN",
                "Readiness passed later than the budget by more than that "
                "resolution. Timing is a warning, never a failure: host noise must "
                "not fail a pipeline.",
            ),
        ),
        "A rolling update waits for readiness before it retires the previous "
        "replica. A slow gate stretches every deploy; a gate that never passes "
        "holds the rollout open and, under maxUnavailable, shrinks live capacity "
        "while it waits.",
        "prestop, in_app, none",
        first_step=(
            "Compare readiness_ready_ms with the configured budget, then read the "
            "target's own startup logs: the time is almost always spent in "
            "dependency connection or migration work before the listener binds."
        ),
    ),
    "SP002": ContractDoc(
        "SP002",
        "Sends ten sequential readiness probes after startup and compares their "
        "outcome signatures, their maximum latency against "
        "contracts.readiness.latency_budget, and — when probes.health is "
        "configured — the stable status, headers and body head against health.",
        (
            "SP001 must first observe successful readiness, so the ten samples "
            "describe steady state rather than the startup transition.",
        ),
        (
            Verdict(
                "stable",
                "PASS",
                "All ten probes returned the configured status inside the latency "
                "budget.",
            ),
            Verdict(
                "same_as_health",
                "WARN",
                "Readiness and health returned identical status, headers and body, "
                "so readiness publishes no routing signal of its own.",
            ),
            Verdict(
                "latency_over_budget",
                "WARN",
                "Every probe was correct, but the slowest exceeded the latency "
                "budget.",
            ),
            Verdict(
                "flapping",
                "FAIL",
                "The ten probes disagreed: readiness alternates between outcomes.",
            ),
            Verdict(
                "incorrect",
                "FAIL",
                "All ten agreed on a status or error other than the configured "
                "one. Consistently wrong rather than unstable.",
            ),
        ),
        "Endpoints membership follows readiness. A flapping endpoint makes the "
        "platform add and remove the replica repeatedly, so traffic is routed to "
        "a process that has just declared itself unfit; a readiness path that is "
        "really a liveness check reports ready before dependencies are usable, and "
        "the first requests after each rollout fail.",
        "prestop, in_app, none",
        first_step=(
            "Replay the readiness path repeatedly from inside the service network "
            "and find what changes between calls — most often a dependency check "
            "that is itself unstable, or a route that answers before warm-up ends."
        ),
    ),
    "SP003": ContractDoc(
        "SP003",
        "Reads PID 1's signal disposition from /proc/1/status before shutdown, "
        "sends SIGTERM explicitly, then combines the observed reactions — readiness "
        "change, accept stop, voluntary exit — with the Docker daemon's kill/die "
        "events. The verdict comes from the observed reaction and the deadline; "
        "exit codes 0, 2, 137 and 143 are recorded as evidence but do not select "
        "a branch.",
        (
            "The target must reach shutdown and Docker must report its exit; "
            "without an observed exit the experiment itself is incomplete.",
        ),
        (
            Verdict(
                "shutdown_observed",
                "PASS",
                "A voluntary reaction to SIGTERM was observed and the process "
                "stopped itself within the budget.",
            ),
            Verdict(
                "shutdown_not_started",
                "FAIL",
                "Nothing reacted: no readiness change, no accept stop, no "
                "voluntary exit after SIGTERM.",
            ),
            Verdict(
                "signal_discarded",
                "FAIL",
                "PID 1 still had the kernel default disposition for SIGTERM. The "
                "init of a PID namespace discards exactly those signals, so the "
                "process never received it. Usually a shell-form CMD without exec.",
            ),
            Verdict(
                "killed",
                "FAIL",
                "A reaction was observed but the handler never finished; SIGKILL "
                "was what actually ended the process.",
            ),
        ),
        "A process that never receives SIGTERM runs unchanged for the entire grace "
        "period and is then SIGKILLed. Every connection it holds dies at the same "
        "instant, and the shutdown work that a graceful stop exists for — flushing "
        "buffers, deregistering from discovery, releasing leases and locks — never "
        "runs at all.",
        "prestop, in_app, none",
        first_step=(
            "Read the pid1 and runtime_handler_installed evidence. If PID 1 is a "
            "shell, switch to exec form or prefix the command with `exec` so the "
            "application becomes PID 1 and can install its own handler."
        ),
    ),
    "SP004": ContractDoc(
        "SP004",
        "Opens a fresh TCP connection to the target every 50ms and sends a real "
        "HTTP request on it, so the measurement is application accept rather than "
        "kernel backlog. Under in_app and none the stream continues past T0 "
        "(SIGTERM) and records the last accepted connection, the resets after it, "
        "and the resulting accept_window_ms; under prestop the stream stops at T0, "
        "because a real preStop rollout has already removed the pod from routing "
        "before the signal is sent.",
        (
            "in_app only — shutdown must visibly start; without an observed "
            "reaction there is no application-owned drain transition to measure.",
            "in_app only — traffic must reach the container directly. A published "
            "port proxy accepts on the application's behalf, so its listener "
            "timestamps describe the proxy, not the process.",
            "in_app only — in_app_window must exceed 20 probe intervals (1000ms at "
            "the 50ms default); a smaller window cannot be separated from the "
            "probe's own resolution.",
            "Every strategy — a shutdown budget of 2s or less must exceed the "
            "measured teardown floor. Longer budgets skip calibration, because the "
            "floor cannot change their verdict.",
        ),
        (
            Verdict(
                "prestop_not_applicable",
                "PASS",
                "The platform hook owns routing removal, so post-T0 listener timing "
                "is not evidence about the application. accept_window_ms is still "
                "reported, as evidence only. This is the only branch prestop "
                "reaches.",
                ("prestop",),
            ),
            Verdict(
                "none_uncovered",
                "WARN",
                "No mechanism covers routing propagation, so connection loss is "
                "expected by declaration and listener behaviour cannot improve the "
                "verdict. This is the only branch none reaches.",
                ("none",),
            ),
            Verdict(
                "in_app_covered",
                "PASS",
                "The listener kept accepting past the declared in_app_window with "
                "more than 20 percent reserve, and readiness published a status "
                "change.",
                ("in_app",),
            ),
            Verdict(
                "in_app_thin_margin",
                "WARN",
                "The listener covered the window, but the reserve is under 20 "
                "percent — close enough that ordinary host noise would breach it.",
                ("in_app",),
            ),
            Verdict(
                "in_app_readiness_not_signaled",
                "WARN",
                "The listener covered the window, but readiness never published a "
                "status change, so load balancers outside Kubernetes have nothing "
                "to observe the drain by.",
                ("in_app",),
            ),
            Verdict(
                "in_app_listener_closed_early",
                "FAIL",
                "The listener stopped accepting before the declared window ended, "
                "while routing is still expected to deliver new connections.",
                ("in_app",),
            ),
            Verdict(
                "accept_then_reset",
                "FAIL",
                "A connection opened after T0 completed its handshake and was then "
                "reset with no response at all. Worse than a refusal: the caller "
                "already believed it was connected. Overrides the window verdict.",
                ("in_app",),
            ),
            Verdict(
                "shutdown_never_started",
                "INCONCLUSIVE",
                "No shutdown reaction was observed, so there is no drain transition "
                "to measure. See SP003 first.",
                ("in_app",),
            ),
            Verdict(
                "port_proxy_likely",
                "INCONCLUSIVE",
                "Traffic went through a published-port proxy — the Docker Desktop "
                "host-fallback path — so listener timing describes the proxy. The "
                "unresolved candidate verdict is kept in evidence.",
                ("in_app",),
            ),
            Verdict(
                "in_app_window_below_probe_resolution",
                "INCONCLUSIVE",
                "in_app_window is not larger than 20 probe intervals, so the "
                "window cannot be distinguished from the probe's own resolution.",
                ("in_app",),
            ),
            Verdict(
                "budget_below_teardown_floor",
                "INCONCLUSIVE",
                "The shutdown budget is inside the measured teardown envelope, so "
                "no timing claim about this run can be separated from Docker's own "
                "teardown cost.",
            ),
        ),
        "Endpoints removal is asynchronous: after SIGTERM the load balancer keeps "
        "opening new connections for as long as the propagation takes. If the "
        "listener has already closed, those arrive at a closed or resetting socket "
        "and reach the caller as 502s and connection resets — during a deploy in "
        "which the process itself exited cleanly and every log looks normal.",
        "prestop, in_app, none",
        (
            "prestop: the platform hook owns routing removal before SIGTERM; the "
            "probe stops at T0 and the contract always passes as not_applicable.",
            "in_app: the application owns the gap. It must keep accepting for "
            "in_app_window after SIGTERM and publish a readiness change, then drain.",
            "none: nothing covers routing propagation, so SP004 warns regardless of "
            "what the listener does.",
        ),
        first_step=(
            "Decide which strategy the deployment actually uses before reading the "
            "numbers. For platform-owned draining add a preStop sleep that covers "
            "propagation; for application-owned draining set --drain in_app and keep "
            "the listener and readiness open for the declared window. "
            "accept_then_reset is different: the socket is being accepted and "
            "dropped, which is usually a worker pool closing its listener while "
            "connections sit in the backlog."
        ),
    ),
    "SP005": ContractDoc(
        "SP005",
        "Measures a steady-state request baseline, launches contracts.inflight."
        "concurrent requests, waits until their sockets are confirmed connected, "
        "sends SIGTERM while they are still unfinished, then classifies every "
        "accepted request as completed, reset before response, reset mid-response, "
        "or timed out. The denominator is what was still open at T0, which is "
        "normally smaller than the configured concurrency. Completion must be 100 "
        "percent.",
        (
            "contracts.inflight must not be explicitly null; null is an intentional "
            "opt-out and therefore SKIP rather than a measurement failure.",
            "When readiness is the zero-config fallback target, its p50 must be at "
            "least 10x the probe-path jitter, so the before/after-SIGTERM boundary "
            "is distinguishable from noise. An explicit --inflight-path bypasses "
            "this gate. Be aware that the fallback is the default path: with no "
            "--inflight-path, SP005 measures whatever the readiness endpoint does, "
            "and a fast readiness endpoint is often close enough to the jitter "
            "floor that the same service resolves on one host and not on another. "
            "Measured: a readiness p50 of 2.5-5.3ms against a 0.23-1.66ms jitter "
            "floor gave ratios from 1.9x to 15.3x on one machine. If SP005 reports "
            "readiness_fallback_below_resolution, that is the tool declining to "
            "guess, not a defect in the service; point --inflight-path at a slower "
            "representative endpoint and the measurement becomes stable.",
            "Shutdown must visibly start, otherwise the request outcomes cannot be "
            "attributed to a shutdown transition at all — this is the precondition "
            "that stops a process which ignores SIGTERM from reporting a clean "
            "sweep.",
            "The pre-signal baseline must be all 2xx, otherwise shutdown would be "
            "blamed for failures that were already present in steady state.",
        ),
        (
            Verdict(
                "all_completed",
                "PASS",
                "Every request that was in flight when SIGTERM arrived received a "
                "complete response.",
            ),
            Verdict(
                "requests_destroyed",
                "FAIL",
                "At least one in-flight request was reset, truncated, or timed out. "
                "broken_requests names each one with its offset from T0.",
            ),
            Verdict(
                "nothing_in_flight",
                "ERROR",
                "SIGTERM was sent while no request was actually open. The window "
                "was empty, so a clean result would have meant nothing; the run "
                "reports the empty window instead of a sweep.",
            ),
            Verdict(
                "disabled",
                "SKIP",
                "contracts.inflight was explicitly set to null. Under --fail-on "
                "this still blocks, because SP005 is a required contract.",
            ),
            # Classified decision_unit because the branch is decided by
            # `readiness_p50 / jitter < MIN_JITTER_RATIO` — two numbers already
            # measured, compared. The image contributes nothing: the same image
            # lands on either side depending on the host's jitter floor. A live
            # fixture measured 1.9x to 15.3x across six runs on one machine and
            # crossed the threshold in two of them, so the container was not
            # proving the comparison, it was rolling for it. See
            # docs/field-notes.md, "A second row, much closer to the boundary".
            Verdict(
                "readiness_fallback_below_resolution",
                "INCONCLUSIVE",
                "No in-flight path was configured and readiness is too fast "
                "relative to jitter to hold a request open across the signal. Pass "
                "--inflight-path with a slower representative endpoint.",
                evidence=Evidence.DECISION_UNIT,
                proof=(
                    "tests/test_preconditions.py::"
                    "test_readiness_fallback_below_jitter_resolution_is_inconclusive"
                ),
            ),
            Verdict(
                "shutdown_never_started",
                "INCONCLUSIVE",
                "The process showed no reaction to SIGTERM, so the measured window "
                "contained no shutdown. Request counts stay in the evidence; the "
                "verdict does not.",
            ),
            Verdict(
                "baseline_not_2xx",
                "INCONCLUSIVE",
                "The steady-state baseline was not all 2xx, so the request path was "
                "already broken before the signal was sent.",
            ),
        ),
        "These are the requests a caller is currently waiting on. Losing them is a "
        "user-visible error on every single rollout, and it is invisible to any "
        "check that reads the exit code: a process can call os._exit(0), report a "
        "clean exit, and destroy every open response on its way out.",
        "prestop, in_app, none",
        (
            "prestop: the hook only prevents new routing before T0. Requests "
            "already accepted must still complete, so the contract is unchanged.",
            "in_app: the application owns both halves — stopping new work and "
            "finishing what it accepted before T0.",
            "none: completion is measured exactly the same way; there is simply no "
            "separate protection for new connections.",
        ),
        first_step=(
            "Read broken_requests and the window evidence before changing anything: "
            "the offsets say whether requests died at the listener close or at the "
            "process exit. A worker pool that finishes only the request in hand and "
            "abandons its queue is the common cause, and worker count — not "
            "graceful-timeout — is what governs it."
        ),
    ),
    "SP006": ContractDoc(
        "SP006",
        "Measures SIGTERM-to-exit from the Docker daemon's own kill/die "
        "timestamps when they are available, falling back to observed wait() "
        "wall-time, and compares it with termination_grace_period minus "
        "pre_stop.duration. An effective SIGKILL is a deadline failure regardless "
        "of the final timestamp.",
        (
            "For budgets of 2s or less the budget must exceed the sidecar-measured "
            "teardown resolution — the median floor plus three sample standard "
            "deviations. Longer budgets skip calibration entirely, because the "
            "floor cannot affect their verdict.",
        ),
        (
            Verdict(
                "within_budget",
                "PASS",
                "The process exited voluntarily with more than 20 percent of the "
                "shutdown budget still unused.",
            ),
            Verdict(
                "thin_margin",
                "WARN",
                "It exited voluntarily, but with less than 20 percent margin. The "
                "line between slow and late.",
            ),
            Verdict(
                "past_deadline",
                "FAIL",
                "It overran the budget, never exited, or was ended by SIGKILL.",
            ),
            Verdict(
                "budget_below_teardown_floor",
                "INCONCLUSIVE",
                "The configured budget is inside the measured teardown envelope, so "
                "the deadline cannot be distinguished from Docker's own overhead.",
            ),
        ),
        "When the grace period expires the platform sends SIGKILL and stops "
        "waiting. In-progress work, buffered writes and unflushed telemetry are "
        "lost at that instant. A thin margin is the same failure one slow "
        "dependency away: the shutdown that passes today is killed the day a "
        "connection pool takes two seconds longer to close.",
        "prestop, in_app, none",
        first_step=(
            "Compare shutdown_duration_ms with margin_ms, then profile the shutdown "
            "path itself. Note that pre_stop.duration is subtracted from the grace "
            "period: a 5s preStop inside a 30s grace leaves the application 25s, "
            "not 30s."
        ),
    ),
}
