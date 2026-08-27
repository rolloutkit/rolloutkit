"""SP003 — signal handling.

The verdict comes from what the process did, not from the number the kernel
printed on its way out. That distinction was not free: the previous version read
the exit code first and every code it did not recognise fell through to PASS, so
a Go binary that destroyed ten in-flight requests and exited 2 was reported as
`clean_exit`. An exit code is a summary written by whoever exited; behaviour is
what we measured.

Three behaviours are separated here. Exit status is retained as evidence, but
does not select a verdict branch:

  * the process ended itself           -> how, and did it report success
  * SIGKILL ended it                   -> it had the signal and did not act
  * SIGTERM never reached it           -> it never had the chance

The third is the one worth the machinery. For the init of a PID namespace the
kernel discards any signal whose disposition is still the default, so an image
whose PID 1 installed no handler does not shut down badly — it does not shut
down at all, and nothing in the exit code says why. `/proc/1/status` says so
before the signal is ever sent.
"""

from __future__ import annotations

from typing import Any

from preflightkit.config.duration import format_measured_ms
from preflightkit.config.models import Platform
from preflightkit.contracts.base import ContractResult, Status
from preflightkit.engine.context import SIGKILL_EXIT, SIGTERM_EXIT, RunReport

_SHELLS = ("/bin/sh", "/bin/bash", "sh", "bash", "/bin/ash", "/usr/bin/env")

_EXPECTED = "the application observes SIGTERM and exits on its own"

#: How large the daemon's own reporting cost has to be, as a share of the
#: measured duration, before it is worth warning the reader about it.
_FLOOR_NOTE_SHARE = 0.1


class SignalContract:
    id = "SP003"
    name = "signal-handling"
    required = True
    PRECONDITIONS = ()

    BRANCHES = {
        "shutdown_not_started": Status.FAIL,
        "signal_discarded": Status.FAIL,
        "killed": Status.FAIL,
        "shutdown_observed": Status.PASS,
    }

    def evaluate(self, report: RunReport) -> ContractResult:
        actual = _behaviour(report)
        evidence = _evidence(report)
        notes = _static_notes(report, evidence) + _measurement_notes(report)
        shutdown_started = _shutdown_started(report)

        def result(
            status: Status, summary: str, branch: str, extra: list[str] | None = None
        ) -> ContractResult:
            return ContractResult(
                self.id,
                self.name,
                status,
                summary,
                branch=branch,
                expected=_EXPECTED,
                actual=actual,
                evidence=evidence,
                notes=notes + (extra or []),
                facts={"shutdown_started": shutdown_started},
            )

        assert report.exit_ns is not None, "the runner requires a daemon exit status"

        duration_ms = report.shutdown_duration_ms
        duration = format_measured_ms(duration_ms or 0)

        if not shutdown_started:
            if report.runtime_handler_installed is False:
                return result(
                    Status.FAIL,
                    f"shutdown never started: PID 1 ({report.pid1_comm or 'unknown'}) "
                    "showed no reaction to SIGTERM",
                    "signal_discarded",
                    _discard_notes(report),
                )
            return result(
                Status.FAIL,
                "shutdown never started: readiness did not change, accepts did "
                "not stop, and no voluntary exit was observed",
                "shutdown_not_started",
                _handler_note(report),
            )

        if report.sigkill_effective:
            return result(
                Status.FAIL,
                f"SIGKILL stopped the process after {duration} — it had SIGTERM "
                "and did not exit on it",
                "killed",
                [
                    "In Kubernetes this is the end of the grace period: every "
                    "connection still open is severed, whatever it was doing."
                ]
                + _handler_note(report),
            )

        return result(
            Status.PASS,
            f"shutdown started and the process stopped within budget after {duration}",
            "shutdown_observed",
        )


def _behaviour(report: RunReport) -> dict[str, Any]:
    """What happened, in terms that do not require reading an exit code."""
    return {
        "ended_by": _ended_by(report),
        "exited_on_its_own": report.exit_code is not None
        and not report.sigkill_effective,
        "signal_deliverable": report.runtime_handler_installed,
        "pid1": report.pid1_comm,
        "runtime_handler_installed": report.runtime_handler_installed,
        "sigterm_ignored": report.sigterm_ignored,
        "shutdown_duration_ms": report.shutdown_duration_ms,
        "shutdown_duration_source": report.shutdown_duration_source,
        "reacted": _reaction_signs(report),
        "shutdown_started": _shutdown_started(report),
    }


def _ended_by(report: RunReport) -> str | None:
    if report.exit_code is None:
        return None
    if report.exit_code == SIGKILL_EXIT:
        return "sigkill"
    if report.exit_code == SIGTERM_EXIT:
        return "sigterm_default_disposition"
    return "its_own_exit"


def _evidence(report: RunReport) -> dict[str, Any]:
    """Everything the verdict was read from, plus what it was read against.

    `exit_code` lives here now. It is a real observation and it belongs in the
    record; it just is not the thing being judged.
    """
    return {
        **_static_evidence(report),
        "exit_code": report.exit_code,
        # `sigkill_sent` is what preflightkit did; `sigkill_effective` is what
        # ended the process. They diverge at short budgets — the enforcer fires,
        # the process was already leaving, and the daemon reports its own status.
        "sigkill_sent": report.sigkill_sent,
        "pid1_status": report.pid1.as_dict() if report.pid1 else None,
        "daemon_shutdown_duration_ms": _round(report.daemon_shutdown_duration_ms),
        "observed_shutdown_duration_ms": _round(report.observed_shutdown_duration_ms),
        "observation_lag_ms": _round(report.observation_lag_ms),
        "teardown_floor_ms": _round(report.teardown_floor_ms),
        "teardown_calibration": report.teardown_calibration.as_dict()
        if report.teardown_calibration is not None
        else None,
        "logs_tail": report.logs_tail[-1000:],
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _stopsignal_mismatch(report: RunReport) -> bool:
    """Kubernetes always sends SIGTERM and ignores the image's STOPSIGNAL.

    Docker honours it. So an image with `STOPSIGNAL SIGQUIT` shuts down correctly
    under `docker stop` and incorrectly under Kubernetes — and nothing in a local
    `docker stop` test would reveal that.
    """
    if report.config.deployment.platform is not Platform.KUBERNETES:
        return False
    stop_signal = str(report.image_config.get("StopSignal") or "SIGTERM").upper()
    return stop_signal not in ("SIGTERM", "15", "TERM")


def _is_shell_form(argv: list[str] | None) -> bool:
    return bool(argv) and len(argv) >= 3 and argv[0] in _SHELLS and argv[1] == "-c"


def _static_evidence(report: RunReport) -> dict[str, Any]:
    config = report.image_config
    entrypoint = config.get("Entrypoint")
    cmd = config.get("Cmd")
    return {
        "stop_signal": config.get("StopSignal") or "SIGTERM (default)",
        "entrypoint": entrypoint,
        "cmd": cmd,
        "entrypoint_shell_form": _is_shell_form(entrypoint),
        "cmd_shell_form": _is_shell_form(cmd) and not entrypoint,
        "docker_init_injected": False,
    }


def _discard_notes(report: RunReport) -> list[str]:
    """Why the signal was dropped — the application's doing, or the kernel's."""
    if report.sigterm_ignored:
        return [
            "PID 1 set SIGTERM to SIG_IGN: the application asked for the signal "
            "to be discarded. Nothing about the grace period changes that."
        ]
    return [
        "PID 1 in a container is the init of a PID namespace, and the kernel "
        "silently discards signals whose disposition is still the default for "
        "it. The application was never woken; a longer grace period would only "
        "make the wait longer. Install a SIGTERM handler, or run the application "
        "as a child of an init that forwards signals.",
    ]


def _handler_note(report: RunReport) -> list[str]:
    if report.runtime_handler_installed is None:
        return [
            "Whether PID 1 had a SIGTERM handler was not measured: no "
            "`busybox:latest` image is present locally, and preflightkit does not "
            "pull on your behalf. `docker pull busybox` turns this branch into a "
            "diagnosis instead of a symptom."
        ]
    return []


def _measurement_notes(report: RunReport) -> list[str]:
    notes: list[str] = []
    floor = report.teardown_floor_ms
    duration = report.shutdown_duration_ms
    # Only worth saying when it is a material share of the number printed. On a
    # four-second wait for a grace period to expire, 13ms of daemon is noise.
    if floor is None or duration is None or floor <= 1:
        pass
    elif floor >= duration:
        notes.append(
            f"The {duration:.0f}ms is at or below this host's floor of "
            f"{floor:.0f}ms: that is what the daemon took to report a container "
            "that could not have taken any time to die — one `sleep` on busybox, "
            "SIGKILLed, in the same network shape as your target. The "
            "application's own shutdown is not resolvable here; all that can be "
            "said is that it cost nothing this measurement could see. Most of the "
            "floor is the published port being torn down, not the process."
        )
    elif floor >= duration * _FLOOR_NOTE_SHARE:
        notes.append(
            f"About {floor:.0f}ms of the {duration:.0f}ms is the daemon tearing "
            "down and reporting rather than the application shutting down: that "
            "is what this host took to report the cheapest possible container — "
            "one `sleep` on busybox, in the same network shape as your target — "
            "dead after a SIGKILL, which cannot be delayed. A lower bound, not a "
            "correction, and never subtracted: a measurement minus an estimate is "
            "a number nothing observed."
        )
    if report.shutdown_duration_source == "observed":
        notes.append(
            "The duration is our own round trip to the daemon at both ends: no "
            "`kill`/`die` frames arrived on the event stream to time it on the "
            "daemon's single clock. Expect it to read a few milliseconds long."
        )
    return notes


def _static_notes(report: RunReport, static: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if static["entrypoint_shell_form"] or static["cmd_shell_form"]:
        notes.append(
            "The entrypoint is shell-form (/bin/sh -c ...). The shell becomes "
            "PID 1 and may not forward SIGTERM to the application. Use exec-form "
            "(JSON array) or `exec` the process."
        )
    if _stopsignal_mismatch(report):
        notes.append(
            f"Image declares STOPSIGNAL {static['stop_signal']}, but Kubernetes "
            "always sends SIGTERM and ignores STOPSIGNAL. This image would behave "
            "differently under `docker stop` than in your cluster."
        )
    if report.sigkill_sent and not report.sigkill_effective:
        notes.append(
            "preflightkit sent SIGKILL when the budget ran out, but the process "
            f"reported exit {report.exit_code} rather than {SIGKILL_EXIT} — it was "
            "already on its way out and ended on its own terms. The verdict "
            "follows the process, not our timer."
        )
    notes.append(
        "preflightkit never enables Docker's --init: tini as PID 1 would change "
        "signal routing, and the measurement would describe tini, not your app."
    )
    return notes


def _reaction_signs(report: RunReport) -> dict[str, bool]:
    return {
        "readiness_changed": report.readiness_drop_ns is not None
        and (report.sigkill_ns is None or report.readiness_drop_ns < report.sigkill_ns),
        "accept_stopped": report.accept_stopped_ns is not None,
        "process_exited": report.exit_ns is not None and not report.sigkill_effective,
    }


def _shutdown_started(report: RunReport) -> bool:
    return any(_reaction_signs(report).values())
