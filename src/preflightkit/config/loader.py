"""Config loading: file, ${VAR} expansion, CLI overrides.

Precedence: CLI flags > environment > preflightkit.yaml > defaults.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from preflightkit.config.models import Config

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
    cwd: Path | None = None,
) -> Config:
    """Build the effective config. Raises ConfigError with a usable message."""
    cwd = cwd or Path.cwd()
    raw: dict[str, Any] = {}

    env = dict(os.environ)
    if config_path is None and env.get("PREFLIGHTKIT_CONFIG"):
        config_path = Path(env["PREFLIGHTKIT_CONFIG"])
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

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_render_validation_error(exc)) from exc


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
