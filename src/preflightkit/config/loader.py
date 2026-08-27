"""Config loading: file, ${VAR} expansion, CLI overrides.

Precedence: CLI flags > environment > preflightkit.yaml > defaults.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from preflightkit import __version__
from preflightkit.config.models import Config, DrainStrategy
from preflightkit.traffic.accept_probe import ACCEPT_PROBE_INTERVAL_MS

DEFAULT_CONFIG_NAMES = ("preflightkit.yaml", "preflightkit.yml")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Configuration is unusable. Maps to exit code 2."""


def _expand(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise ConfigError(
                    f"config references ${{{name}}} but that variable is not set"
                )
            return env[name]

        return _VAR.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    """Read a dotenv-style file. Deliberately minimal: KEY=VALUE, # comments."""
    if not path.is_file():
        raise ConfigError(f"env_file not found: {path}")
    result: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE, got {raw!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


def parse_env_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """Turn repeated ``--env KEY=VALUE`` into a mapping, in the order given.

    A later ``--env`` for the same key wins, which is what a reader of the
    command line expects and what Docker does.
    """
    result: dict[str, str] = {}
    for item in pairs:
        key, sep, value = item.partition("=")
        if not sep:
            raise ConfigError(f"--env expects KEY=VALUE, got {item!r}")
        key = key.strip()
        if not key:
            raise ConfigError(f"--env has an empty variable name: {item!r}")
        result[key] = value
    return result


def find_config_file(start: Path) -> Path | None:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = start / name
        if candidate.is_file():
            return candidate
    return None


def load_config(
    *,
    config_path: Path | None = None,
    image: str | None = None,
    port: int | None = None,
    ready_url: str | None = None,
    inflight_path: str | None = None,
    grace: str | None = None,
    drain: str | None = None,
    env_values: Sequence[str] | None = None,
    env_files: Sequence[Path] | None = None,
    cwd: Path | None = None,
) -> Config:
    """Build the effective config. Raises ConfigError with a usable message."""
    cwd = cwd or Path.cwd()
    raw: dict[str, Any] = {}

    env = dict(os.environ)
    if config_path is None and env.get("PREFLIGHTKIT_CONFIG"):
        config_path = Path(env["PREFLIGHTKIT_CONFIG"])

    # The process environment stands in for the flags when they are absent, the
    # way it does for every other option. Split exactly as the CLI splits them,
    # so `PREFLIGHTKIT_ENV` means the same thing to `validate` as to `test`.
    if env_values is None:
        env_values = env.get("PREFLIGHTKIT_ENV", "").split()
    if env_files is None:
        env_files = [
            Path(item)
            for item in env.get("PREFLIGHTKIT_ENV_FILE", "").split(os.pathsep)
            if item
        ]

    # Inline values beat files, both beat the config file's own `env_file`, and
    # both are visible to ${VAR} expansion the way a file's env_file already is
    # — otherwise a value passed on the command line could not be referenced
    # from the config it was passed alongside.
    command_line_env: dict[str, str] = {}
    for item in env_files:
        command_line_env.update(read_env_file(Path(item)))
    command_line_env.update(parse_env_pairs(env_values))
    env.update(command_line_env)
    path = config_path or find_config_file(cwd)
    if config_path is not None and not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    if path is not None:
        try:
            loaded = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        raw = loaded

    target = raw.setdefault("target", {})
    if not isinstance(target, dict):
        raise ConfigError("target must be a mapping")

    # env_file is merged before expansion so ${VAR} can reference it. Service
    # files are intentionally scoped to that service; one dependency must not
    # silently change expansion in the target or another dependency.
    env = _merge_env_file(target, env)
    services = raw.get("services") or {}
    if not isinstance(services, dict):
        raise ConfigError("services must be a mapping")
    for name, service in services.items():
        if not isinstance(service, dict):
            raise ConfigError(f"services.{name} must be a mapping")
        service_env = _merge_env_file(service, env)
        services[name] = _expand(service, service_env)

    raw = _expand(raw, env)
    target = raw["target"]

    # Applied after expansion: a ${...} that survived the shell was meant
    # literally. Only the target is touched — a dependency in `services` keeps
    # its own declared environment, exactly as its own env_file does.
    if command_line_env:
        target["env"] = {**(target.get("env") or {}), **command_line_env}

    # Explicit command-line values win over process environment. Both override
    # the file, while model defaults fill anything the file omits.
    image = image if image is not None else env.get("PREFLIGHTKIT_IMAGE")
    port = port if port is not None else env.get("PREFLIGHTKIT_PORT")  # type: ignore[assignment]
    ready_url = ready_url if ready_url is not None else env.get("PREFLIGHTKIT_READY_URL")
    inflight_path = (
        inflight_path
        if inflight_path is not None
        else env.get("PREFLIGHTKIT_INFLIGHT_PATH")
    )
    grace = grace if grace is not None else env.get("PREFLIGHTKIT_GRACE")
    drain = drain if drain is not None else env.get("PREFLIGHTKIT_DRAIN")

    if image is not None:
        target["image"] = image
    if port is not None:
        target["port"] = port
    if ready_url is not None:
        raw.setdefault("probes", {}).setdefault("readiness", {})["path"] = ready_url
    if inflight_path is not None:
        contracts = raw.setdefault("contracts", {})
        if not isinstance(contracts, dict):
            raise ConfigError("contracts must be a mapping")
        if not isinstance(contracts.get("inflight"), dict):
            contracts["inflight"] = {}
        request = contracts["inflight"].setdefault("request", {})
        if not isinstance(request, dict):
            raise ConfigError("contracts.inflight.request must be a mapping")
        request["path"] = inflight_path
        request.setdefault("expected_duration", "5s")
    if grace is not None:
        raw.setdefault("deployment", {})["termination_grace_period"] = grace
    if drain is not None:
        raw.setdefault("deployment", {}).setdefault("drain", {})["strategy"] = drain

    if not target.get("image"):
        raise ConfigError(
            "no target image: pass one on the command line or set target.image"
        )
    if not target.get("port"):
        raise ConfigError(
            "no target port: pass --port or set target.port "
            "(there is no safe default to guess here)"
        )

    _reject_removed_settings(raw)
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_render_validation_error(exc)) from exc
    _reject_unmeasurable_drain_window(config)
    _reject_unreachable_startup_budget(config)
    return config


#: How many accept-probe samples a declared window has to span before the probe
#: can tell the window apart from its own sampling interval.
MIN_WINDOW_PROBE_SAMPLES = 20


#: Settings that were declared in a shipped model and have been taken out
#: again, mapped to what a reader needs to hear. `extra="forbid"` already stops
#: them, but "Extra inputs are not permitted" reads as a typo; a setting that
#: was in the schema and is now gone deserves to be named as such.
REMOVED_SETTINGS: dict[tuple[str, ...], str] = {
    ("timeouts", "overall"): (
        "It was declared but enforced nowhere, so no run was ever bounded by "
        "it. Making it work is new behaviour, and v0.1 semantics are frozen; "
        "leaving it in the schema would be a second release of a setting that "
        "does nothing. Deferred to v0.2 — see docs/v0.2.md.\n"
        "Delete the line. Startup and shutdown are bounded by timeouts.startup "
        "and timeouts.shutdown, which are enforced."
    ),
}


def _reject_removed_settings(raw: dict[str, Any]) -> None:
    """Name a setting that this version no longer has, instead of ignoring it."""
    for path, explanation in REMOVED_SETTINGS.items():
        section: Any = raw
        for key in path[:-1]:
            if not isinstance(section, dict):
                break
            section = section.get(key)
        else:
            if isinstance(section, dict) and path[-1] in section:
                dotted = ".".join(path)
                raise ConfigError(
                    f"{dotted} is not a setting in preflightkit "
                    f"{__version__}.\n{explanation}"
                )


def _reject_unmeasurable_drain_window(config: Config) -> None:
    """Refuse an in_app window the accept probe could never resolve.

    This is not a verdict about the target. Both sides of the comparison are
    known before anything runs — a declared window against a fixed probe
    interval — so nothing is learned by starting a container to discover it.
    Answering it here costs nothing and answers immediately; SP004 used to
    answer it after a full run, having pulled an image, started it, sent a
    signal and waited for the exit, only to report INCONCLUSIVE.
    """
    drain = config.deployment.drain
    if drain.strategy is not DrainStrategy.IN_APP:
        return
    floor_ms = MIN_WINDOW_PROBE_SAMPLES * ACCEPT_PROBE_INTERVAL_MS
    if drain.in_app_window > floor_ms:
        return
    raise ConfigError(
        f"deployment.drain.in_app_window is {drain.in_app_window}ms, which is "
        f"not greater than the {floor_ms}ms the accept probe can resolve "
        f"({MIN_WINDOW_PROBE_SAMPLES} samples at {ACCEPT_PROBE_INTERVAL_MS}ms). "
        "A window that small cannot be distinguished from the probe's own "
        "sampling, so no listener timing measured across it would mean "
        "anything.\n"
        f"Raise deployment.drain.in_app_window above {floor_ms}ms — 5s is the "
        "common Kubernetes value — or declare a strategy that does not depend "
        "on listener timing: prestop delegates the gap to the platform, none "
        "reports it as uncovered."
    )


def _reject_unreachable_startup_budget(config: Config) -> None:
    """Refuse a startup budget the run can never reach a verdict about.

    contracts.startup.budget is a threshold SP001 warns on; timeouts.startup is
    the wall the run dies against, with exit 3 and nothing measured. Order them
    the wrong way round and the warning becomes unreachable code: a container
    slower than the budget is also past the wall, so it aborts as an
    infrastructure error and SP001's over_budget branch never runs. Both numbers
    are in the config, so this is answerable before anything starts.
    """
    budget = config.contracts.startup.budget
    wall = config.timeouts.startup
    if budget < wall:
        return
    raise ConfigError(
        f"contracts.startup.budget is {budget}ms and timeouts.startup is "
        f"{wall}ms: the budget must be shorter than the timeout.\n"
        "The budget is a threshold SP001 warns about; the timeout is the wall "
        "the run aborts against with exit 3 and nothing measured. With the "
        "budget at or past the wall, a container slow enough to exceed the "
        "budget has already been killed by the timeout, so SP001 can never "
        "report it.\n"
        f"Lower contracts.startup.budget below {wall}ms, or raise "
        f"timeouts.startup above {budget}ms."
    )


def _render_validation_error(exc: ValidationError) -> str:
    lines = ["invalid configuration:"]
    for err in exc.errors():
        location = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  {location}: {err['msg']}")
    return "\n".join(lines)


def _merge_env_file(section: dict[str, Any], expansion_env: dict[str, str]) -> dict[str, str]:
    env_file = section.get("env_file")
    if env_file is None:
        return expansion_env
    paths = env_file if isinstance(env_file, list) else [env_file]
    file_env: dict[str, str] = {}
    for item in paths:
        file_env.update(read_env_file(Path(item)))
    merged = dict(file_env)
    merged.update(section.get("env") or {})
    section["env"] = merged
    return {**expansion_env, **file_env}
