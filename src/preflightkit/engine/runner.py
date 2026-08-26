"""Orchestrates a session: N runs, contract evaluation, aggregation."""

from __future__ import annotations

from collections.abc import Callable

from preflightkit.config.models import Config
from preflightkit.contracts import ALL_CONTRACTS
from preflightkit.engine.events import now_ns
from preflightkit.engine.lifecycle import StartupFailure, run_experiment
from preflightkit.engine.preconditions import evaluate_contracts
from preflightkit.evidence.model import RunOutcome, Session, new_run_id
from preflightkit.runtime.docker import DockerError, DockerRuntime


class InfrastructureError(Exception):
    """Docker or the target environment failed us. Exit code 3, never a FAIL.

    Keeping this separate from contract verdicts is deliberate: "the app could not
    start because a dependency was missing" is not the same finding as "the app
    destroys in-flight requests", and conflating them makes both useless.
    """

    def __init__(self, message: str, logs: str = "") -> None:
        super().__init__(message)
        self.logs = logs


async def run_session(
    config: Config,
    *,
    repeat: int = 1,
    evaluate: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Session:
    """Run the experiment `repeat` times and evaluate the contracts.

    Anything Docker refuses to do — a missing image, a container that will not
    start, an API that answers with an error — is an infrastructure problem, not
    a bug in preflightkit and not a verdict about the image. Letting a
    DockerError escape as an internal error would tell the user to file a bug
    when what they actually need to do is build the image.
    """
    try:
        return await _run_session(
            config, repeat=repeat, evaluate=evaluate, progress=progress
        )
    except DockerError as exc:
        raise InfrastructureError(str(exc)) from exc


async def _run_session(
    config: Config,
    *,
    repeat: int,
    evaluate: bool,
    progress: Callable[[str], None] | None,
) -> Session:
    session = Session(run_id=new_run_id(), image=config.target.image)
    async with DockerRuntime(progress=progress) as runtime:
        for index in range(repeat):
            started = now_ns()
            try:
                report = await run_experiment(
                    config, runtime, progress=_run_progress(progress, index, repeat)
                )
            except StartupFailure as exc:
                raise InfrastructureError(str(exc), logs=exc.logs) from exc
            outcome = RunOutcome(
                report=report,
                duration_ms=(now_ns() - started) / 1_000_000,
            )
            if evaluate:
                outcome.results = evaluate_contracts(report, ALL_CONTRACTS)
            session.runs.append(outcome)
    return session


def _run_progress(
    progress: Callable[[str], None] | None, index: int, repeat: int
) -> Callable[[str], None] | None:
    """Number the phases when there is more than one run to tell apart.

    Under `--repeat 3` the same seven phase lines appear three times; without the
    prefix they read as one run that keeps restarting.
    """
    if progress is None or repeat == 1:
        return progress
    return lambda message: progress(f"run {index + 1}/{repeat}: {message}")
