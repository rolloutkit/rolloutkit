"""Command line interface."""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console

from preflightkit import __version__
from preflightkit.config.loader import ConfigError, load_config
from preflightkit.contracts.base import ContractResult, Status
from preflightkit.engine.runner import InfrastructureError, run_session
from preflightkit.evidence.redact import Redactor
from preflightkit.evidence.model import Session
from preflightkit.reporters import json_out, terminal
from preflightkit.runtime.docker import DockerError
from preflightkit.runtime.socket import DockerUnavailable

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Measure how a container actually behaves during startup and shutdown.",
)
console = Console()
err_console = Console(stderr=True)


class ExitCode:
    OK = 0
    CONTRACTS_VIOLATED = 1
    CONFIG_ERROR = 2
    INFRASTRUCTURE_ERROR = 3
    INTERNAL_ERROR = 4


class FailOn(StrEnum):
    NONE = "none"
    ERROR = "error"
    WARN = "warn"


class Format(StrEnum):
    TERMINAL = "terminal"
    JSON = "json"


#: Failures that mean "the experiment could not run", wherever they surface.
INFRASTRUCTURE = (DockerUnavailable, InfrastructureError, DockerError)


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten an ExceptionGroup down to the exceptions that actually happened.

    Everything runs inside anyio task groups, so an error from a background task
    arrives wrapped. Reporting the wrapper gives the user "ExceptionGroup:
    unhandled errors in a TaskGroup (1 sub-exception)" — a sentence that names
    neither what failed nor where, and that cost a debugging session to see past.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def _describe(exc: BaseException) -> str:
    seen: list[str] = []
    for leaf in _leaves(exc):
        text = f"{type(leaf).__name__}: {leaf}".strip().rstrip(":")
        if text not in seen:
            seen.append(text)
    return "\n".join(seen) or f"{type(exc).__name__}: {exc}"


#: FLAKY is absent on purpose — an unstable verdict never blocks a pipeline.
BLOCKING = {
    FailOn.NONE: set(),
    FailOn.ERROR: {Status.FAIL, Status.ERROR},
    FailOn.WARN: {Status.FAIL, Status.ERROR, Status.WARN},
}


@app.command()
def test(
    image: Annotated[str | None, typer.Argument(help="Image to test.")] = None,
    port: Annotated[int | None, typer.Option("--port", "-p")] = None,
    ready_url: Annotated[str | None, typer.Option("--ready-url")] = None,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    fail_on: Annotated[FailOn, typer.Option("--fail-on")] = FailOn.NONE,
    allow_inconclusive: Annotated[
        bool,
        typer.Option(
            "--allow-inconclusive",
            help="Permit required contracts to be SKIP or INCONCLUSIVE while gating.",
        ),
    ] = False,
    output: Annotated[Format, typer.Option("--format")] = Format.TERMINAL,
    repeat: Annotated[int, typer.Option("--repeat", min=1, max=20)] = 1,
) -> None:
    """Run the lifecycle experiment and evaluate the contracts.

    Report-only by default. A tool that once blocked a release wrongly gets
    removed from the pipeline and never comes back — trust first, gate second.
    """
    try:
        config = load_config(
            config_path=config_path, image=image, port=port, ready_url=ready_url
        )
    except ConfigError as exc:
        err_console.print(f"[bold red]config error[/]\n{exc}")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc

    try:
        session = anyio.run(lambda: run_session(config, repeat=repeat))
    except INFRASTRUCTURE as exc:
        err_console.print(f"[bold red]infrastructure error[/]\n{exc}")
        _print_logs(config, getattr(exc, "logs", ""))
        raise typer.Exit(ExitCode.INFRASTRUCTURE_ERROR) from exc
    except Exception as exc:  # noqa: BLE001
        # A daemon failure raised inside a task group is still a daemon failure.
        # Classifying by the wrapper would report "Docker went away" as a bug in
        # preflightkit, and exit 4 tells CI to look in the wrong place.
        leaves = _leaves(exc)
        if leaves and all(isinstance(leaf, INFRASTRUCTURE) for leaf in leaves):
            err_console.print(f"[bold red]infrastructure error[/]\n{_describe(exc)}")
            _print_logs(config, "")
            raise typer.Exit(ExitCode.INFRASTRUCTURE_ERROR) from exc
        err_console.print(f"[bold magenta]internal error[/]\n{_describe(exc)}")
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from exc

    if output is Format.JSON:
        sys.stdout.write(json_out.dump(session, __version__) + "\n")
    else:
        terminal.render(session, __version__, console)

    violated = _blocking_results(session, fail_on, allow_inconclusive)
    if violated:
        raise typer.Exit(ExitCode.CONTRACTS_VIOLATED)
    if fail_on is FailOn.NONE and output is Format.TERMINAL:
        worst = session.verdict_status
        if worst not in (Status.PASS, Status.SKIP):
            console.print(
                "[dim]report-only: exit 0. Use --fail-on error to block CI.[/]"
            )
    raise typer.Exit(ExitCode.OK)


def _blocking_results(
    session: Session, fail_on: FailOn, allow_inconclusive: bool
) -> list[ContractResult]:
    blocking = BLOCKING[fail_on]
    violated = [result for result in session.aggregated if result.status in blocking]
    if fail_on is not FailOn.NONE and not allow_inconclusive:
        seen = {result.id for result in violated}
        violated.extend(
            result
            for result in session.required_unmeasured
            if result.id not in seen
        )
    return violated


@app.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


if __name__ == "__main__":
    app()


def _print_logs(config, logs: str) -> None:
    """Show why the container died. Without this the answer is unreachable.

    The container is removed as soon as the run ends, so these lines are the only
    copy. They are printed with markup disabled — log output is full of things
    like `[INFO]` that rich would otherwise eat — and with env values redacted,
    because an application that fails at startup tends to print its own
    configuration on the way out.
    """
    if not logs.strip():
        return
    redactor = Redactor(config.secret_values())
    err_console.print("\n[dim]--- container output (last lines) ---[/]")
    err_console.print(redactor.text(logs.rstrip()), markup=False, highlight=False)
