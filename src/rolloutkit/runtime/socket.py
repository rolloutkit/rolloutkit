"""Locating the Docker daemon socket.

Docker Desktop on macOS does not use /var/run/docker.sock as its active endpoint
even when that path exists — the current *context* points somewhere else. Reading
the context store directly avoids shelling out to `docker context inspect`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


class DockerUnavailable(Exception):
    """No usable Docker endpoint. Maps to exit code 3."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    socket_path: str
    source: str  # how we found it — goes into evidence


def _docker_config_dir() -> Path:
    return Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")


def _from_env() -> Endpoint | None:
    host = os.environ.get("DOCKER_HOST")
    if not host:
        return None
    if not host.startswith("unix://"):
        raise DockerUnavailable(
            f"DOCKER_HOST={host!r} is not a unix socket. "
            "rolloutkit v0.1 talks to the daemon over a unix socket only; "
            "TCP and SSH endpoints are not supported yet."
        )
    return Endpoint(host.removeprefix("unix://"), "DOCKER_HOST")


def _from_context() -> Endpoint | None:
    config_dir = _docker_config_dir()
    config_file = config_dir / "config.json"
    if not config_file.is_file():
        return None
    try:
        name = json.loads(config_file.read_text()).get("currentContext")
    except (json.JSONDecodeError, OSError):
        return None
    if not name or name == "default":
        return None

    digest = hashlib.sha256(name.encode()).hexdigest()
    meta = config_dir / "contexts" / "meta" / digest / "meta.json"
    if not meta.is_file():
        return None
    try:
        host = json.loads(meta.read_text())["Endpoints"]["docker"]["Host"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None
    if not isinstance(host, str) or not host.startswith("unix://"):
        return None
    return Endpoint(host.removeprefix("unix://"), f"docker context {name!r}")


def _wellknown() -> list[Endpoint]:
    return [
        Endpoint(str(Path.home() / ".docker" / "run" / "docker.sock"), "Docker Desktop default"),
        Endpoint("/var/run/docker.sock", "system default"),
    ]


def discover_endpoint() -> Endpoint:
    """First candidate whose socket actually exists.

    Existence is checked, not connectivity — a stale socket file still fails at
    connect time, and that error is more informative than ours would be.
    """
    candidates: list[Endpoint] = []
    from_env = _from_env()
    if from_env is not None:
        candidates.append(from_env)
    from_context = _from_context()
    if from_context is not None:
        candidates.append(from_context)
    candidates.extend(_wellknown())

    for candidate in candidates:
        if Path(candidate.socket_path).is_socket():
            return candidate

    tried = "\n".join(f"  {c.socket_path}  ({c.source})" for c in candidates)
    raise DockerUnavailable(
        "no Docker daemon socket found. Tried:\n"
        f"{tried}\n"
        "Is Docker running? On macOS, start Docker Desktop."
    )
