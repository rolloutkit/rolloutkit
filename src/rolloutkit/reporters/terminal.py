"""Terminal report.

The layout is the product: a timeline with offsets and a list of what broke. No
verdict appears without the measurement behind it.
"""

from __future__ import annotations

from collections import Counter

from rich.console import Console
from rich.text import Text

from rolloutkit.config.duration import format_measured_ms, format_ms
from rolloutkit.config.models import Config
from rolloutkit.contracts.base import ContractResult, Status
from rolloutkit.engine.context import RunReport
from rolloutkit.engine.events import Kind
from rolloutkit.evidence.model import RunOutcome, Session
from rolloutkit.evidence.redact import Redactor

STATUS_STYLE = {
    Status.PASS: "bold green",
    Status.FAIL: "bold red",
    Status.WARN: "bold yellow",
    Status.SKIP: "dim",
    Status.INCONCLUSIVE: "bold yellow",
    Status.ERROR: "bold magenta",
    Status.FLAKY: "bold cyan",
}

_TIMELINE_LABELS = {
    str(Kind.SIGNAL_SENT): "T0  SIGTERM -> PID 1",
    str(Kind.READINESS_FAILED): "T1  readiness",
    str(Kind.PROCESS_EXITED): "T4  process exit",
    str(Kind.REQUEST_RESET): "    request destroyed",
    str(Kind.CONNECTION_REFUSED): "    connection refused",
    str(Kind.TIMEOUT_REACHED): "    budget exhausted -> SIGKILL",
}

LABEL_WIDTH = 46

#: Per-request events printed in the timeline before the rest are summarised.
_MAX_EVENTS_PER_KIND = 5

#: Evidence rows printed per contract. The timeline used to promise that the
#: events it elided were "all listed under CONTRACTS", and CONTRACTS obliged by
#: printing 134 of them — the cap was applied where the output was already
#: readable and skipped where it was not. Both blocks now point at the JSON,
#: which is the only place that genuinely holds everything.
_MAX_EVIDENCE_ROWS = 5

#: The timeline has room for what happened, not for the full classification —
#: that lives in the contract evidence.
_SHORT_OUTCOME = {
    "reset_before_response": "no response",
    "reset_mid_response": "cut mid-response",
    "timeout": "never completed",
    "refused": "refused",
}


def render(session: Session, version: str, console: Console | None = None) -> None:
    console = console or Console()
    if not session.runs:
        console.print(f"[bold red]no run completed[/] {session.infrastructure_error or ''}")
        return

    last = session.runs[-1]
    config = last.report.config
    redactor = Redactor(config.secret_values())

    console.print()
    console.print(f"[bold]rolloutkit {version}[/]   run {session.run_id}")
    console.print(f"Target: {session.image}")
    console.print(_profile_line(config))
    if len(session.runs) > 1:
        console.print(f"Repeats: {len(session.runs)}")
    console.print()

    _startup_block(console, last)
    _baseline_block(console, last.report)
    _timeline_block(console, last.report, redactor)
    _contracts_block(console, session, redactor)
    _environment_block(console, last.report)
    _verdict_line(console, session)


def render_measurement(
    session: Session, version: str, console: Console | None = None
) -> None:
    """Render only observations; ``measure`` deliberately has no verdicts."""
    console = console or Console()
    if not session.runs:
        console.print(f"[bold red]no run completed[/] {session.infrastructure_error or ''}")
        return
    last = session.runs[-1]
    config = last.report.config
    redactor = Redactor(config.secret_values())
    console.print()
    console.print(f"[bold]rolloutkit {version}[/]   measurement {session.run_id}")
    console.print(f"Target: {session.image}")
    console.print(_profile_line(config))
    if len(session.runs) > 1:
        console.print(f"Repeats: {len(session.runs)}")
    console.print()
    _startup_block(console, last)
    _baseline_block(console, last.report)
    _timeline_block(console, last.report, redactor)
    _environment_block(console, last.report)


def _profile_line(config: Config) -> str:
    deployment = config.deployment
    parts = [str(deployment.platform), f"grace {format_ms(deployment.termination_grace_period)}"]
    if deployment.pre_stop.duration:
        parts.append(f"preStop {deployment.pre_stop.type} {format_ms(deployment.pre_stop.duration)}")
    parts.append(f"drain {deployment.drain.strategy}")
    return (
        "Profile: "
        + ", ".join(parts)
        + f" -> shutdown budget {format_ms(deployment.shutdown_budget_ms)}"
    )


def _startup_block(console: Console, run: RunOutcome) -> None:
    report = run.report
    console.print("[bold]STARTUP[/]")
    tcp = _delta_ms(report.container_started_ns, report.tcp_open_ns)
    ready = report.startup_duration_ms
    port = report.config.target.port
    console.print(
        "  startup resolution".ljust(LABEL_WIDTH)
        + _ms(report.startup_resolution_ms)
    )
    if report.tcp_open_is_meaningful:
        console.print(f"  TCP :{port} open".ljust(LABEL_WIDTH) + _ms(tcp))
    else:
        console.print(
            f"  TCP :{port} open".ljust(LABEL_WIDTH) + "INCONCLUSIVE"
        )
        console.print(
            Text(
                "        via a port proxy (Docker Desktop) - this is not proof "
                "the application was listening",
                style="dim",
            )
        )
    path = report.config.probes.readiness.path
    console.print(
        f"  GET {path} -> {report.readiness_status}".ljust(LABEL_WIDTH) + _ms(ready)
    )
    _pid1_line(console, report)
    console.print()


def _pid1_line(console: Console, report: RunReport) -> None:
    """Printed in STARTUP because it is known in STARTUP.

    The one line in this report that predicts rather than describes. A PID 1 with
    no SIGTERM handler will not shut down when signalled, and /proc/1/status says
    so while the container is still healthy - before the grace period is spent
    proving it.
    """
    if report.pid1 is None:
        return
    if report.sigterm_ignored:
        disposition = "SIGTERM set to SIG_IGN - it will be discarded"
    elif report.runtime_handler_installed:
        disposition = "SIGTERM handler installed"
    else:
        disposition = "no SIGTERM handler - the kernel will discard it"
    console.print(
        "  PID 1 signal disposition".ljust(LABEL_WIDTH)
        + f"{report.pid1_comm or '?'}, {disposition}"
    )


def _timeline_block(console: Console, report: RunReport, redactor: Redactor) -> None:
    if report.sigterm_ns is None:
        return
    console.print("[bold]SHUTDOWN TIMELINE[/]")
    events = [
        event
        for event in report.bus.ordered()
        if event.kind in _TIMELINE_LABELS
        and event.offset_ms_from(report.sigterm_ns) >= -1
    ]
    # With `concurrent: 200` the per-request events would bury the phase markers
    # that make the timeline readable. The cap is stated rather than applied
    # silently, and it names the one output that really does hold every event.
    totals = Counter(event.kind for event in events)
    shown: Counter[str] = Counter()
    for event in events:
        shown[event.kind] += 1
        if shown[event.kind] > _MAX_EVENTS_PER_KIND:
            if shown[event.kind] == _MAX_EVENTS_PER_KIND + 1:
                extra = totals[event.kind] - _MAX_EVENTS_PER_KIND
                console.print(
                    f"      ... and {extra} more (--format json lists every one)"
                )
            continue
        detail = _timeline_detail(event.kind, event.data, redactor)
        line = f"  {_TIMELINE_LABELS[event.kind]}"
        if detail:
            line += f" {detail}"
        if len(line) > LABEL_WIDTH:
            line = line[: LABEL_WIDTH - 2] + ".."
        offset = event.offset_ms_from(report.sigterm_ns)
        console.print(line.ljust(LABEL_WIDTH) + f"{offset:+.0f}ms")
    if report.readiness_drop_ns is None:
        console.print("  T1  readiness -> never".ljust(LABEL_WIDTH) + "never")
    t2 = report.offset_ms(report.last_accepted_ns)
    resolution = report.accept_probe_interval_ms or None
    accepted = (
        f"{t2:+.0f}ms (±{resolution:.0f}ms)"
        if t2 is not None and resolution is not None
        else (f"{t2:+.0f}ms" if t2 is not None else "not measured")
    )
    console.print(
        "  T2  last new connection accepted".ljust(LABEL_WIDTH)
        + accepted
    )
    console.print()


def _baseline_block(console: Console, report: RunReport) -> None:
    """What the service did before anyone shut it down.

    Printed even on a clean run, because it is the answer to "were the requests
    you measured actually working requests?" — and because `sigterm_after` is now
    usually derived rather than configured, which the reader is entitled to see.
    """
    baseline = report.baseline
    inflight = report.config.contracts.inflight
    if baseline is None or inflight is None:
        return
    console.print("[bold]BASELINE[/] [dim](before the signal)[/]")
    statuses = ", ".join(f"{k}" for k in sorted(baseline.statuses)) or "no response"
    console.print(
        f"  {inflight.request.method} {report.inflight_path} x{baseline.samples}".ljust(
            LABEL_WIDTH
        )
        + f"{baseline.succeeded}/{baseline.samples} ok -> {statuses}"
    )
    if baseline.p50_ms is not None:
        console.print(
            "  request duration p50 / p90".ljust(LABEL_WIDTH)
            + f"{_ms(baseline.p50_ms)} / {_ms(baseline.p90_ms)}"
        )
    if report.sigterm_after_ms is not None:
        source = (
            "derived from p50"
            if report.sigterm_after_source in ("baseline", "readiness_fallback")
            else "from config"
        )
        console.print(
            f"  sigterm_after ({source})".ljust(LABEL_WIDTH)
            + _ms(report.sigterm_after_ms)
        )
    console.print()


def _timeline_detail(kind: str, data: dict, redactor: Redactor) -> str:
    if kind == str(Kind.READINESS_FAILED):
        # "-> 503" and "unreachable" are different findings: the first is the app
        # actively draining, the second is the app simply gone.
        if data.get("status") is not None:
            return f"-> {data['status']}"
        return "unreachable (no connection)"
    if kind == str(Kind.PROCESS_EXITED):
        return f"(code {data.get('exit_code')})"
    if kind == str(Kind.REQUEST_RESET):
        outcome = str(data.get("outcome"))
        return f"#{data.get('request_id')} {_SHORT_OUTCOME.get(outcome, outcome)}"
    if kind == str(Kind.CONNECTION_REFUSED):
        return f"#{data.get('request_id')}"
    return ""


def _contracts_block(console: Console, session: Session, redactor: Redactor) -> None:
    console.print("[bold]CONTRACTS[/]")
    for result in session.aggregated:
        badge = Text(f"{result.status:<5}", style=STATUS_STYLE[result.status])
        line = Text(f"  {result.id} {result.name:<22} ")
        line.append(badge)
        line.append("  " + redactor.text(result.summary))
        console.print(line)
        _evidence_block(console, result, redactor)
    console.print()


def _evidence_block(console: Console, result: ContractResult, redactor: Redactor) -> None:
    """Destroyed-request rows, then notes.

    The rows describe a verdict that went against the target, so PASS and SKIP
    have none to show. Notes are not tied to the verdict that way: SP004 can pass
    and still have watched connections be reset after its window closed. Printing
    those only under a red status would make the report agree with itself rather
    than with the run, so a note is printed wherever it exists.
    """
    if result.status not in (Status.PASS, Status.SKIP):
        _broken_requests(console, result)
    for note in result.notes:
        console.print(Text(f"        - {redactor.text(note)}", style="dim"))


def _broken_requests(console: Console, result: ContractResult) -> None:
    broken = result.evidence.get("broken_requests") or []
    for item in broken[:_MAX_EVIDENCE_ROWS]:
        offset = item.get("offset_ms")
        offset_text = f"{offset:+.0f}ms" if offset is not None else "?"
        bytes_note = ""
        if item.get("expected_body_bytes"):
            bytes_note = f", {item['body_bytes']}/{item['expected_body_bytes']} bytes"
        line = (
            f"        request #{item['request_id']} {item['outcome']} "
            f"during {item['phase']}{bytes_note}"
        )
        console.print(f"{line:<{LABEL_WIDTH + 6}} {offset_text}")
    if len(broken) > _MAX_EVIDENCE_ROWS:
        extra = len(broken) - _MAX_EVIDENCE_ROWS
        console.print(
            Text(
                f"        ... and {extra} more destroyed requests "
                "(--format json lists every one)",
                style="dim",
            )
        )


def _environment_block(console: Console, report: RunReport) -> None:
    jitter = report.measurement_jitter_ms
    bits = [f"host {report.host_os}"]
    if report.docker_server.get("version"):
        bits.append(f"docker {report.docker_server['version']}/{report.docker_server.get('os')}")
    if report.cpu_count:
        bits.append(f"{report.cpu_count} cpu")
    if report.load_average:
        bits.append("load %.2f" % report.load_average[0])
    bits.append(f"probe {report.probe_location}")
    if jitter is not None:
        bits.append(f"probe-path jitter ~{jitter:.1f}ms")
    if report.teardown_calibration_status == "not_calibrated":
        bits.append("teardown not_calibrated")
    console.print(Text("  " + " | ".join(bits), style="dim"))
    if report.probe_fallback_reason:
        console.print(
            Text(
                f"  Sidecar unavailable; using host fallback: "
                f"{report.probe_fallback_reason}",
                style="yellow",
            )
        )
    for wait in report.dependency_waits:
        if wait.outcome == "skipped":
            console.print(
                Text(
                    f"  Dependency gate skipped for {wait.service}:{wait.port} — "
                    f"{wait.skip_reason}",
                    style="yellow",
                )
            )
        elif wait.waited_ms is not None:
            console.print(
                Text(
                    f"  Waited {wait.waited_ms:.0f}ms for {wait.service}:"
                    f"{wait.port} to accept connections",
                    style="dim",
                )
            )
    if jitter is not None:
        console.print(
            Text(
                f"  Timestamps come from monotonic_ns; application-facing "
                f"observations are precise to about {jitter:.1f}ms at "
                f"{report.probe_location}, not to the nanosecond.",
                style="dim",
            )
        )
    for line in _provenance(report):
        console.print(Text("  " + line, style="dim"))
    console.print()


def _provenance(report: RunReport) -> list[str]:
    """Where T4 came from, and what it still contains.

    Two separate caveats, and neither is optional. The first is that the timeline
    is drawn on our clock while the duration is taken from the daemon's, so the
    two disagree by a round trip. The second is larger: the daemon needs time to
    notice a death, and that time is inside every duration below regardless of
    which clock produced it.
    """
    lines: list[str] = []
    daemon = report.daemon_shutdown_duration_ms
    observed = report.observed_shutdown_duration_ms
    if daemon is not None and observed is not None:
        lines.append(
            f"T4 is the daemon's own kill->die pair ({daemon:.0f}ms), one clock "
            f"with no round trip of ours in it. The timeline above is drawn on "
            f"our clock, which saw the exit at {observed:.0f}ms."
        )
    floor = report.teardown_floor_ms
    if floor is not None and floor > 1:
        calibration = report.teardown_calibration
        spread = (
            f", spread {calibration.stddev_ms:.1f}ms, resolution threshold "
            f"{calibration.resolution_threshold_ms:.0f}ms"
            if calibration is not None
            else ""
        )
        teardown_subject = (
            "the published-port proxy and network"
            if report.port_proxy_likely
            else "the run-scoped bridge network"
        )
        lines.append(
            f"The cheapest possible container - one sleep, same network shape as "
            f"your target - took {floor:.0f}ms to be reported dead on this host "
            f"after a SIGKILL it could not delay. At least that much of any "
            f"duration above is the daemon tearing down {teardown_subject}, not "
            f"the application shutting down{spread}."
        )
    return lines


def _verdict_line(console: Console, session: Session) -> None:
    counts: dict[Status, int] = {}
    for result in session.aggregated:
        counts[result.status] = counts.get(result.status, 0) + 1
    parts = [
        f"{count} {status}"
        for status, count in sorted(counts.items(), key=lambda kv: str(kv[0]))
        if status not in (Status.PASS, Status.SKIP, Status.INCONCLUSIVE)
    ]
    summary = ", ".join(parts) if parts else "all measured contracts passed"
    style = STATUS_STYLE[session.verdict_status]
    console.print(Text(f"Result: {summary}", style=style))
    unmeasured = session.required_unmeasured
    if unmeasured:
        noun = "contract" if len(unmeasured) == 1 else "contracts"
        statuses = Counter(result.status for result in unmeasured)
        detail = ", ".join(
            f"{count} {status}" for status, count in sorted(statuses.items())
        )
        console.print(
            Text(
                f"Warning: {len(unmeasured)} required {noun} did not produce a "
                f"verdict ({detail}). Gating blocks unless "
                "--allow-inconclusive is set.",
                style=STATUS_STYLE[Status.INCONCLUSIVE],
            )
        )


def _delta_ms(start: int | None, end: int | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start) / 1_000_000


def _ms(value: float | None) -> str:
    return "-" if value is None else format_measured_ms(value)
