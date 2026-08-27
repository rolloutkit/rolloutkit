"""Configuration model.

The deployment profile is not decoration: it defines the shutdown budget used
by SP006. A missing profile gets the documented Kubernetes 30-second default so
the one-line CLI still produces a useful verdict.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from preflightkit.config.duration import Duration
from preflightkit.evidence.redact import names_a_secret


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Platform(StrEnum):
    KUBERNETES = "kubernetes"
    DOCKER = "docker"
    ECS = "ecs"
    NOMAD = "nomad"


class PreStopType(StrEnum):
    NONE = "none"
    SLEEP = "sleep"
    EXEC = "exec"


class DrainStrategy(StrEnum):
    PRESTOP = "prestop"
    IN_APP = "in_app"
    NONE = "none"


class InflightMode(StrEnum):
    LONG_REQUESTS = "long_requests"
    CONTINUOUS_LOAD = "continuous_load"


class Target(Strict):
    image: str
    port: int = Field(ge=1, le=65535)
    env: dict[str, str] = Field(default_factory=dict)
    env_file: Path | list[Path] | None = None
    command: list[str] | None = None


class Service(Strict):
    """A dependency reachable only inside the run-scoped bridge network."""

    image: str
    env: dict[str, str] = Field(default_factory=dict)
    env_file: Path | list[Path] | None = None
    command: list[str] | None = None


class PreStop(Strict):
    type: PreStopType = PreStopType.NONE
    duration: Duration = 0

    @model_validator(mode="after")
    def _sleep_needs_duration(self) -> PreStop:
        if self.type is PreStopType.SLEEP and self.duration <= 0:
            raise ValueError("pre_stop.type 'sleep' requires a non-zero duration")
        return self


class Drain(Strict):
    strategy: DrainStrategy = DrainStrategy.NONE
    in_app_window: Duration = 0

    @model_validator(mode="after")
    def _in_app_needs_window(self) -> Drain:
        if self.strategy is DrainStrategy.IN_APP and self.in_app_window <= 0:
            raise ValueError(
                "drain.strategy 'in_app' requires a non-zero in_app_window"
            )
        return self


class Deployment(Strict):
    platform: Platform = Platform.KUBERNETES
    termination_grace_period: Duration = 30_000
    pre_stop: PreStop = PreStop()
    drain: Drain = Drain()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def shutdown_budget_ms(self) -> int:
        """What the app actually gets: the grace period minus what preStop ate.

        terminationGracePeriodSeconds is counted from the moment the pod is marked
        for deletion, not from SIGTERM. The preStop hook runs inside that window,
        so a 5s sleep leaves 25s of a 30s grace period.
        """
        return self.termination_grace_period - self.pre_stop.duration

    @model_validator(mode="after")
    def _budget_must_be_positive(self) -> Deployment:
        if self.shutdown_budget_ms <= 0:
            raise ValueError(
                "pre_stop.duration consumes the entire termination_grace_period; "
                "no shutdown budget is left for the application"
            )
        return self


class Probe(Strict):
    path: str = "/ready"
    expected_status: int = 200


class Probes(Strict):
    readiness: Probe = Probe()
    health: Probe | None = None


class TrafficProbe(Strict):
    """The run-scoped container that originates lifecycle traffic."""

    image: str = "python:3.12-slim"


class StartupContract(Strict):
    budget: Duration = 15_000


class ReadinessContract(Strict):
    latency_budget: Duration = 500


class InflightRequest(Strict):
    method: str = "GET"
    #: None means use the readiness endpoint as the zero-config fallback.
    path: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    expected_duration: Duration = 5_000


class InflightContract(Strict):
    mode: InflightMode = InflightMode.LONG_REQUESTS
    request: InflightRequest = InflightRequest()
    concurrent: int = Field(default=10, ge=1, le=200)
    #: When unset, derived from the baseline burst: half the measured p50. Left
    #: as a guess it is the single hardest number in the config to get right —
    #: too late and every request has already finished, too early and none has
    #: started. The service knows the answer; ask it instead.
    sigterm_after: Duration | None = None

    @model_validator(mode="after")
    def _sigterm_must_land_mid_request(self) -> InflightContract:
        if self.sigterm_after is None:
            return self
        if self.sigterm_after >= self.request.expected_duration:
            raise ValueError(
                f"sigterm_after ({self.sigterm_after}ms) must be shorter than "
                f"request.expected_duration ({self.request.expected_duration}ms), "
                "otherwise the requests finish before the signal and the contract "
                "proves nothing"
            )
        return self


class Contracts(Strict):
    startup: StartupContract = StartupContract()
    readiness: ReadinessContract = ReadinessContract()
    #: Omitted means readiness fallback. Explicit YAML null disables SP005.
    inflight: InflightContract | None = InflightContract()


class Timeouts(Strict):
    # There is no `overall` here on purpose. It was declared through v0.1's
    # development and enforced nowhere, so setting it changed nothing; giving it
    # a meaning now would be new behaviour against frozen semantics. `loader`
    # rejects a configuration that still names it rather than ignoring it a
    # second time. See docs/v0.2.md.

    #: The hard wall, not a contract: crossing it aborts the run with exit 3 and
    #: nothing measured, where crossing contracts.startup.budget only warns. It
    #: is therefore sized off the slowest legitimate startup this project has
    #: observed, with 3x headroom: 26180.60ms for service-a against cold
    #: ephemeral dependencies on native Linux (docs/field-notes.md), which the
    #: old 30s wall cleared by 1.15x — close enough that one cold dependency
    #: pull turns a measurable run into an infrastructure error. 3x of 26.18s
    #: is 78.5s, rounded up to 90s. For scale at the other end: the stdlib
    #: fixture reads 211.57ms on the 2-core CI runner, and service-b's Django
    #: image under a 2-CPU quota reads 6.87-7.63s.
    startup: Duration = 90_000
    shutdown: Duration = 45_000


class Config(Strict):
    version: int = 1
    target: Target
    services: dict[str, Service] = Field(default_factory=dict)
    deployment: Deployment = Deployment()
    probes: Probes = Probes()
    probe: TrafficProbe = TrafficProbe()
    contracts: Contracts = Contracts()
    timeouts: Timeouts = Timeouts()

    def secret_values(self) -> list[str]:
        """Values that must never reach a report verbatim.

        Env values are filtered by variable *name* — see `redact.names_a_secret`.
        Masking every value hid a hostname in a crash log and cost more than it
        protected. Headers are not filtered: a header carrying a credential is
        rarely named after one.
        """
        secrets = [v for k, v in self.target.env.items() if v and names_a_secret(k)]
        for service in self.services.values():
            secrets += [
                value
                for key, value in service.env.items()
                if value and names_a_secret(key)
            ]
        if self.contracts.inflight is not None:
            secrets += [v for v in self.contracts.inflight.request.headers.values() if v]
        return secrets
