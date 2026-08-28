"""Command line interface."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console
from rich.padding import Padding

from rolloutkit import __version__
from rolloutkit.config.loader import ConfigError, load_config
from rolloutkit.config.models import Config, DrainStrategy
from rolloutkit.config.compose import import_compose, render_import
from rolloutkit.contracts import ALL_CONTRACTS
from rolloutkit.contracts.base import ContractResult, Status
from rolloutkit.contracts.catalog import ANY_STRATEGY, CATALOG, Verdict
from rolloutkit.engine.runner import InfrastructureError, run_session
from rolloutkit.evidence.redact import Redactor
from rolloutkit.evidence.model import Session
from rolloutkit.reporters import json_out, junit, terminal
from rolloutkit.provenance import rolloutkit_commit
from rolloutkit.runtime.docker import DockerError, ProbePackagingError
from rolloutkit.runtime.socket import DockerUnavailable

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
    JUNIT = "junit"


class MeasureFormat(StrEnum):
    TERMINAL = "terminal"
    JSON = "json"


#: Failures that mean "the experiment could not run", wherever they surface.
#: `ProbePackagingError` is one of them and is also caught on its own below, so
#: that a broken installation is not reported as a bad afternoon for Docker.
INFRASTRUCTURE = (
    DockerUnavailable,
    InfrastructureError,
    DockerError,
    ProbePackagingError,
)


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


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(_version_text())
        raise typer.Exit(ExitCode.OK)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and source commit.",
        ),
    ] = False,
) -> None:
    """Measure container lifecycle behavior."""


@app.command()
def test(
    image: Annotated[str | None, typer.Argument(help="Image to test.")] = None,
    port: Annotated[
        int | None, typer.Option("--port", "-p", envvar="ROLLOUTKIT_PORT")
    ] = None,
    ready_url: Annotated[
        str | None, typer.Option("--ready-url", envvar="ROLLOUTKIT_READY_URL")
    ] = None,
    inflight_path: Annotated[
        str | None,
        typer.Option("--inflight-path", envvar="ROLLOUTKIT_INFLIGHT_PATH"),
    ] = None,
    grace: Annotated[
        str | None, typer.Option("--grace", envvar="ROLLOUTKIT_GRACE")
    ] = None,
    drain: Annotated[
        DrainStrategy | None,
        typer.Option("--drain", envvar="ROLLOUTKIT_DRAIN"),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            envvar="ROLLOUTKIT_ENV",
            help="KEY=VALUE for the target container; repeat for more.",
        ),
    ] = None,
    env_file: Annotated[
        list[Path] | None,
        typer.Option(
            "--env-file",
            envvar="ROLLOUTKIT_ENV_FILE",
            help="Read KEY=VALUE lines for the target; repeat for more. --env wins.",
        ),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", envvar="ROLLOUTKIT_CONFIG")
    ] = None,
    fail_on: Annotated[
        FailOn, typer.Option("--fail-on", envvar="ROLLOUTKIT_FAIL_ON")
    ] = FailOn.NONE,
    allow_inconclusive: Annotated[
        bool,
        typer.Option(
            "--allow-inconclusive",
            envvar="ROLLOUTKIT_ALLOW_INCONCLUSIVE",
            help="Permit required contracts to be SKIP or INCONCLUSIVE while gating.",
        ),
    ] = False,
    output: Annotated[
        Format, typer.Option("--format", envvar="ROLLOUTKIT_FORMAT")
    ] = Format.TERMINAL,
    repeat: Annotated[
        int,
        typer.Option("--repeat", min=1, max=20, envvar="ROLLOUTKIT_REPEAT"),
    ] = 1,
) -> None:
    """Run the lifecycle experiment and evaluate the contracts.

    Report-only by default. A tool that once blocked a release wrongly gets
    removed from the pipeline and never comes back — trust first, gate second.
    """
    try:
        config = load_config(
            config_path=config_path,
            image=image,
            port=port,
            ready_url=ready_url,
            inflight_path=inflight_path,
            grace=grace,
            drain=str(drain) if drain is not None else None,
            env_values=env,
            env_files=env_file,
        )
    except ConfigError as exc:
        _config_error(exc)

    session = _run(config, repeat=repeat, evaluate=True)

    if output is Format.JSON:
        sys.stdout.write(json_out.dump(session, __version__) + "\n")
    elif output is Format.JUNIT:
        sys.stdout.write(junit.dump(session, __version__) + "\n")
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


@app.command()
def measure(
    image: Annotated[str | None, typer.Argument(help="Image to measure.")] = None,
    port: Annotated[
        int | None, typer.Option("--port", "-p", envvar="ROLLOUTKIT_PORT")
    ] = None,
    ready_url: Annotated[
        str | None, typer.Option("--ready-url", envvar="ROLLOUTKIT_READY_URL")
    ] = None,
    inflight_path: Annotated[
        str | None,
        typer.Option("--inflight-path", envvar="ROLLOUTKIT_INFLIGHT_PATH"),
    ] = None,
    grace: Annotated[
        str | None, typer.Option("--grace", envvar="ROLLOUTKIT_GRACE")
    ] = None,
    drain: Annotated[
        DrainStrategy | None,
        typer.Option("--drain", envvar="ROLLOUTKIT_DRAIN"),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            envvar="ROLLOUTKIT_ENV",
            help="KEY=VALUE for the target container; repeat for more.",
        ),
    ] = None,
    env_file: Annotated[
        list[Path] | None,
        typer.Option(
            "--env-file",
            envvar="ROLLOUTKIT_ENV_FILE",
            help="Read KEY=VALUE lines for the target; repeat for more. --env wins.",
        ),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", envvar="ROLLOUTKIT_CONFIG")
    ] = None,
    repeat: Annotated[
        int,
        typer.Option("--repeat", min=1, max=20, envvar="ROLLOUTKIT_REPEAT"),
    ] = 1,
    output: Annotated[
        MeasureFormat, typer.Option("--format", envvar="ROLLOUTKIT_FORMAT")
    ] = MeasureFormat.TERMINAL,
) -> None:
    """Collect measurements and a timeline without contract verdicts or gating."""
    try:
        config = load_config(
            config_path=config_path,
            image=image,
            port=port,
            ready_url=ready_url,
            inflight_path=inflight_path,
            grace=grace,
            drain=str(drain) if drain is not None else None,
            env_values=env,
            env_files=env_file,
        )
    except ConfigError as exc:
        _config_error(exc)
    session = _run(config, repeat=repeat, evaluate=False)
    if output is MeasureFormat.JSON:
        sys.stdout.write(json_out.dump(session, __version__) + "\n")
    else:
        terminal.render_measurement(session, __version__, console)
    raise typer.Exit(ExitCode.OK)


@app.command()
def validate(
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", envvar="ROLLOUTKIT_CONFIG")
    ] = None,
    image: Annotated[str | None, typer.Option("--image")] = None,
    port: Annotated[int | None, typer.Option("--port", "-p")] = None,
    ready_url: Annotated[str | None, typer.Option("--ready-url")] = None,
    inflight_path: Annotated[str | None, typer.Option("--inflight-path")] = None,
    grace: Annotated[str | None, typer.Option("--grace")] = None,
    drain: Annotated[DrainStrategy | None, typer.Option("--drain")] = None,
    env: Annotated[list[str] | None, typer.Option("--env")] = None,
    env_file: Annotated[list[Path] | None, typer.Option("--env-file")] = None,
) -> None:
    """Validate configuration without contacting Docker."""
    try:
        config = load_config(
            config_path=config_path,
            image=image,
            port=port,
            ready_url=ready_url,
            inflight_path=inflight_path,
            grace=grace,
            drain=str(drain) if drain is not None else None,
            env_values=env,
            env_files=env_file,
        )
    except ConfigError as exc:
        _config_error(exc)
    console.print(
        f"configuration valid: {config.target.image}:{config.target.port}; "
        f"platform={config.deployment.platform}, "
        f"grace={config.deployment.termination_grace_period}ms, "
        f"drain={config.deployment.drain.strategy}"
    )


def _print_verdicts(verdicts: Sequence[Verdict], *, indent: str) -> None:
    """Render one branch per row: the id a report prints, then what it means."""
    for verdict in verdicts:
        console.print(f"{indent}{verdict.status:<13}{verdict.branch}")
        # Padding, not a leading space run: it indents wrapped lines too, so a
        # long meaning stays visibly attached to its branch.
        console.print(Padding(verdict.meaning, (0, 0, 0, len(indent) + 2)))


@app.command()
def explain(
    contract_id: Annotated[str, typer.Argument(help="Contract id, for example SP004.")]
) -> None:
    """Explain a contract from static, offline documentation."""
    key = contract_id.upper()
    doc = CATALOG.get(key)
    if doc is None:
        err_console.print(
            f"[bold red]unknown contract[/] {contract_id}; use list-contracts"
        )
        raise typer.Exit(ExitCode.CONFIG_ERROR)
    contract = next(item for item in ALL_CONTRACTS if item.id == key)
    console.print(f"[bold]{doc.id} — {contract.name}[/]")
    console.print(f"Measures: {doc.measures}")
    console.print("Preconditions:")
    for condition in doc.preconditions:
        console.print(f"  - {condition}")
    if doc.strategy_dependent:
        console.print("Verdicts (branch, by drain strategy):")
        for strategy in ANY_STRATEGY:
            rows = [
                verdict
                for verdict in doc.verdicts
                if verdict.applies_to != ANY_STRATEGY and strategy in verdict.applies_to
            ]
            if not rows:
                continue
            console.print(f"  {strategy}:")
            _print_verdicts(rows, indent="    ")
        shared = [v for v in doc.verdicts if v.applies_to == ANY_STRATEGY]
        if shared:
            console.print("  any strategy:")
            _print_verdicts(shared, indent="    ")
    else:
        console.print("Verdicts (branch):")
        _print_verdicts(doc.verdicts, indent="  ")
    if doc.precedence:
        console.print(
            "Precedence — several of these hold on the same run, so they are "
            "asked in this order and the first to answer is the verdict:"
        )
        for position, branch in enumerate(doc.precedence, start=1):
            status = next(v.status for v in doc.verdicts if v.branch == branch)
            console.print(f"  {position}. {branch} ({status})")
    console.print(f"Why it matters: {doc.why}")
    if doc.strategy_notes:
        console.print("Drain strategies:")
        for note in doc.strategy_notes:
            console.print(f"  - {note}")
    console.print(f"First step after FAIL: {doc.first_step}")


@app.command("list-contracts")
def list_contracts() -> None:
    """List contract identity, required status, and drain applicability."""
    console.print("ID     NAME                    REQUIRED  STRATEGIES")
    for contract in ALL_CONTRACTS:
        doc = CATALOG[contract.id]
        required = "yes" if contract.required else "no"
        console.print(
            f"{contract.id:<6} {contract.name:<23} {required:<9} {doc.strategies}"
        )


@app.command()
def init(
    from_compose: Annotated[
        Path, typer.Option("--from-compose", help="Read one Docker Compose file.")
    ],
    service: Annotated[str, typer.Option("--service", help="Service to import.")],
    output_path: Annotated[
        Path,
        typer.Option("--output", "-o", help="Generated rolloutkit config path."),
    ] = Path("rolloutkit.yaml"),
) -> None:
    """Generate configuration from one Compose service; Compose is not run."""
    if output_path.exists():
        _config_error(ConfigError(f"refusing to overwrite existing file: {output_path}"))
    try:
        imported = import_compose(from_compose, service)
    except ConfigError as exc:
        _config_error(exc)
    output_path.write_text(render_import(imported))
    for warning in imported.warnings:
        err_console.print(f"[bold yellow]warning[/] {warning}", soft_wrap=True)
    console.print(f"generated {output_path} from service {service}; Compose was not run")


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
    console.print(_version_text())


def _config_error(exc: ConfigError) -> None:
    err_console.print(f"[bold red]config error[/]\n{exc}")
    raise typer.Exit(ExitCode.CONFIG_ERROR) from exc


def _run(config: Config, *, repeat: int, evaluate: bool) -> Session:
    try:
        return anyio.run(
            lambda: run_session(
                config,
                repeat=repeat,
                evaluate=evaluate,
                progress=lambda message: err_console.print(message),
            )
        )
    except ProbePackagingError as exc:
        err_console.print(f"[bold red]broken installation[/]\n{exc}")
        raise typer.Exit(ExitCode.INFRASTRUCTURE_ERROR) from exc
    except INFRASTRUCTURE as exc:
        err_console.print(f"[bold red]infrastructure error[/]\n{exc}")
        _print_logs(config, getattr(exc, "logs", ""))
        raise typer.Exit(ExitCode.INFRASTRUCTURE_ERROR) from exc
    except Exception as exc:  # noqa: BLE001
        leaves = _leaves(exc)
        if leaves and all(isinstance(leaf, INFRASTRUCTURE) for leaf in leaves):
            err_console.print(f"[bold red]infrastructure error[/]\n{_describe(exc)}")
            _print_logs(config, "")
            raise typer.Exit(ExitCode.INFRASTRUCTURE_ERROR) from exc
        err_console.print(f"[bold magenta]internal error[/]\n{_describe(exc)}")
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from exc


def _version_text() -> str:
    return f"rolloutkit {__version__} ({rolloutkit_commit()})"


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
