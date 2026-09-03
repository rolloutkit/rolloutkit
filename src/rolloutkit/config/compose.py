"""One-way Docker Compose configuration import.

Compose is never used at runtime. This module reads one file and produces the
small rolloutkit profile the selected service needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rolloutkit.config.loader import ConfigError


@dataclass(frozen=True, slots=True)
class ComposeImport:
    document: dict[str, Any]
    warnings: tuple[str, ...]
    build_notes: tuple[str, ...]


def import_compose(path: Path, service_name: str) -> ComposeImport:
    if not path.is_file():
        raise ConfigError(f"Compose file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid Compose YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: Compose top level must be a mapping")
    services = raw.get("services")
    if not isinstance(services, dict):
        raise ConfigError(f"{path}: services must be a mapping")
    selected = services.get(service_name)
    if not isinstance(selected, dict):
        names = ", ".join(sorted(str(name) for name in services)) or "none"
        raise ConfigError(
            f"service {service_name!r} not found in {path}; available: {names}"
        )

    warnings: list[str] = []
    build_notes: list[str] = []
    _warn_override_files(path, warnings)
    _warn_unsupported(raw, "Compose file", warnings)
    target = _target(path, service_name, selected, warnings, build_notes)
    dependencies = _dependency_names(service_name, selected, warnings)
    imported_services: dict[str, Any] = {}
    for dependency_name in dependencies:
        dependency = services.get(dependency_name)
        if not isinstance(dependency, dict):
            raise ConfigError(
                f"service {service_name!r} depends on missing service "
                f"{dependency_name!r}"
            )
        _warn_unsupported(dependency, f"service {dependency_name}", warnings)
        imported_services[dependency_name] = _service(
            path, dependency_name, dependency, warnings, build_notes
        )

    document: dict[str, Any] = {"version": 1, "target": target}
    if imported_services:
        document["services"] = imported_services
    document.update(
        {
            "deployment": {
                "termination_grace_period": "30s",
                "pre_stop": {"type": "none"},
                "drain": {"strategy": "none"},
            },
            "probes": {"readiness": {"path": "/ready"}},
            "contracts": {"inflight": {"request": {"path": None}}},
        }
    )
    return ComposeImport(document, tuple(warnings), tuple(build_notes))


def render_import(result: ComposeImport) -> str:
    text = yaml.safe_dump(
        result.document,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    text = text.replace(
        "deployment:\n",
        "deployment:\n  # TODO: set the real production deployment values\n",
        1,
    )
    text = text.replace(
        "  readiness:\n    path: /ready\n",
        "  readiness:\n    # TODO: set the real readiness path\n    path: /ready\n",
        1,
    )
    text = text.replace(
        "  inflight:\n    request:\n      path: null\n",
        "  inflight:\n    # TODO: set a slow endpoint; readiness is used while path is null\n"
        "    request:\n      path: null\n",
        1,
    )
    if result.document["target"].get("image") is None:
        text = text.replace(
            "  image: null\n",
            "  # TODO: build and tag the Compose service, then set its image\n"
            "  image: null\n",
            1,
        )
    if result.document["target"].get("port") is None:
        text = text.replace(
            "  port: null\n",
            "  # TODO: set the container port exposed by the service\n"
            "  port: null\n",
            1,
        )
    if result.build_notes:
        notes = "".join(f"# Compose build source (not executed): {note}\n" for note in result.build_notes)
        text = notes + text
    return text


def _target(
    compose_path: Path,
    name: str,
    service: dict[str, Any],
    warnings: list[str],
    build_notes: list[str],
) -> dict[str, Any]:
    _warn_unsupported(service, f"service {name}", warnings)
    result = _service(compose_path, name, service, warnings, build_notes)
    result["port"] = _port(name, service.get("ports"), warnings)
    return result


def _service(
    compose_path: Path,
    name: str,
    service: dict[str, Any],
    warnings: list[str],
    build_notes: list[str],
) -> dict[str, Any]:
    image = service.get("image")
    build = service.get("build")
    if build is not None:
        build_notes.append(f"{name}: {_build_description(build)}")
    if not image:
        if build is not None:
            warnings.append(
                f"service {name} has build but no image; rolloutkit does not "
                "build images, so target.image needs a TODO value"
            )
        else:
            warnings.append(f"service {name} has neither image nor build")
    imported: dict[str, Any] = {"image": str(image) if image else None}
    environment = _environment(name, service.get("environment"), warnings)
    if environment:
        imported["env"] = environment
    env_file = service.get("env_file")
    if env_file is not None:
        imported["env_file"] = _env_files(compose_path, name, env_file, warnings)
    return imported


def _environment(
    name: str, value: Any, warnings: list[str]
) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {
            str(key): f"${{{key}}}" if item is None else str(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            key, separator, raw = str(item).partition("=")
            if not key:
                warnings.append(f"service {name} has an empty environment entry")
                continue
            result[key] = raw if separator else f"${{{key}}}"
        return result
    raise ConfigError(f"service {name}.environment must be a mapping or list")


def _env_files(
    compose_path: Path, name: str, value: Any, warnings: list[str]
) -> str | list[str]:
    """The paths Compose points at, resolved, and said out loud if absent.

    The reference is imported, never the contents: inlining the variables would
    copy whatever is in that file — credentials included — into a generated
    config that is about to be committed next to the Compose file it came from.

    A reference to a file that is not there is worth saying now rather than at
    run time. It is the ordinary case, not a rare one: `env_file` names a file
    Compose users are told to copy in or generate, so it is exactly the file a
    fresh checkout does not have. The run already refuses it with exit 2 and
    names the field, which is the right failure and arrives after the config
    has been written, read and run — and everything needed to say it is
    knowable the moment the path is resolved.
    """
    entries = value if isinstance(value, list) else [value]
    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("path")
        if not isinstance(entry, str) or not entry:
            raise ConfigError(f"service {name}.env_file contains an invalid path")
        resolved = (compose_path.parent / entry).resolve()
        if not resolved.is_file():
            warnings.append(
                f"service {name}.env_file names {entry}, which does not exist; "
                "the reference is imported as written, so the generated config "
                "will be refused until that file is there or the variables it "
                "would have set are written into the config's own env"
            )
        paths.append(str(resolved))
    return paths[0] if len(paths) == 1 else paths


def _port(name: str, value: Any, warnings: list[str]) -> int | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ConfigError(f"service {name}.ports must be a list")
    if len(value) > 1:
        warnings.append(
            f"service {name} publishes {len(value)} ports; only the first "
            "container port is imported"
        )
    if not value:
        return None
    first = value[0]
    if isinstance(first, dict):
        target = first.get("target")
    else:
        token = str(first).split("/", 1)[0]
        target = token.rsplit(":", 1)[-1]
    try:
        port = int(target)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"service {name}.ports[0] has no numeric target port") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"service {name}.ports[0] target port is out of range")
    return port


def _dependency_names(
    name: str, service: dict[str, Any], warnings: list[str]
) -> list[str]:
    """The services to import, with a warning for every one of them.

    No dependency arrives with a gate, whichever form declared it: Compose
    records what to start before what, never a port, so `wait_for.tcp` is
    always hand-written. The warning is per dependency rather than per file
    because it names the field to write, and the field names the service.

    `depends_on: [db]` and `depends_on: {db: {condition: ...}}` are the same
    omission with different amounts of evidence. The second says the author
    already knew the dependency needed waiting for and asked Compose to do it,
    so it gets the sentence that says so; the first gets the plain one. Warning
    only on the second — which is what this did until a real Compose file
    turned up using the list form — leaves the common case silent about the
    thing most likely to make the run blame the wrong container.
    """
    value = service.get("depends_on")
    if value is None:
        return []
    if isinstance(value, list):
        dependencies = [str(item) for item in value]
        conditioned: set[str] = set()
    elif isinstance(value, dict):
        dependencies = [str(item) for item in value]
        conditioned = {
            str(dependency)
            for dependency, settings in value.items()
            if isinstance(settings, dict) and "condition" in settings
        }
    else:
        raise ConfigError(f"service {name}.depends_on must be a list or mapping")

    for dependency in dependencies:
        if dependency in conditioned:
            warnings.append(
                f"service {name}.depends_on.{dependency}.condition is not "
                f"imported; write services.{dependency}.wait_for.tcp with "
                f"the port {dependency} listens on to wait for it"
            )
        else:
            warnings.append(
                f"service {name} starts after {dependency} but does not wait "
                f"for it; write services.{dependency}.wait_for.tcp with the "
                f"port {dependency} listens on"
            )
    return dependencies


def _warn_unsupported(
    section: dict[str, Any], label: str, warnings: list[str]
) -> None:
    messages = {
        "extends": "extends is not imported (v0.2)",
        "profiles": "profiles are not imported (v0.2)",
        "volumes": "volumes are not imported (v0.2)",
        "healthcheck": (
            "healthcheck is not imported; for a dependency, wait on the port "
            "it listens on with services.<name>.wait_for.tcp"
        ),
    }
    for key, message in messages.items():
        if key in section:
            warnings.append(f"{label}: {message}")


def _warn_override_files(path: Path, warnings: list[str]) -> None:
    """Report conventional sibling overrides that this one-file import ignores."""
    names = {
        "compose.yaml": ("compose.override.yaml", "compose.override.yml"),
        "compose.yml": ("compose.override.yaml", "compose.override.yml"),
        "docker-compose.yaml": (
            "docker-compose.override.yaml",
            "docker-compose.override.yml",
        ),
        "docker-compose.yml": (
            "docker-compose.override.yaml",
            "docker-compose.override.yml",
        ),
    }
    for name in names.get(path.name, ()):
        candidate = path.with_name(name)
        if candidate.is_file():
            warnings.append(
                f"override file {candidate.name} is not imported (v0.2); "
                f"only {path.name} was read"
            )


def _build_description(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        context = value.get("context", ".")
        dockerfile = value.get("dockerfile")
        return f"{context} ({dockerfile})" if dockerfile else str(context)
    return str(value)
