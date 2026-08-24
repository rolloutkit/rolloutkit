"""Orchestrates a session: N runs, contract evaluation, aggregation."""

from __future__ import annotations

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


async def run_session(config: Config, *, repeat: int = 1) -> Session:
    """Run the experiment `repeat` times and evaluate the contracts.

    Anything Docker refuses to do — a missing image, a container that will not
    start, an API that answers with an error — is an infrastructure problem, not
    a bug in preflightkit and not a verdict about the image. Letting a
    DockerError escape as an internal error would tell the user to file a bug
    when what they actually need to do is build the image.
    """
    try:
        return await _run_session(config, repeat=repeat)
    except DockerError as exc:
        raise InfrastructureError(str(exc)) from exc


async def _run_session(config: Config, *, repeat: int) -> Session:
    session = Session(run_id=new_run_id(), image=config.target.image)
    async with DockerRuntime() as runtime:
        for _ in range(repeat):
            started = now_ns()
            try:
                report = await run_experiment(config, runtime)
            except StartupFailure as exc:
                raise InfrastructureError(str(exc), logs=exc.logs) from exc
            outcome = RunOutcome(
                report=report,
                duration_ms=(now_ns() - started) / 1_000_000,
            )
            outcome.results = evaluate_contracts(report, ALL_CONTRACTS)
            session.runs.append(outcome)
    return session
