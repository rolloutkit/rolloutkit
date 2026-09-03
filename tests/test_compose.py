"""One-way Docker Compose import; Compose itself is never invoked."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from rolloutkit.cli.main import app
from rolloutkit.config.loader import load_config

runner = CliRunner()


def test_init_from_compose_generates_a_working_config_with_todos(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "api.env"
    env_file.write_text("API_TOKEN=from-file\n")
    compose = tmp_path / "docker-compose.yml"
    (tmp_path / "docker-compose.override.yml").write_text(
        "services: {api: {environment: {OVERRIDE_ONLY: '1'}}}\n"
    )
    compose.write_text(
        """
services:
  api:
    image: registry.example/api:1.2
    build: {context: ./api, dockerfile: Dockerfile.prod}
    environment:
      LOG_LEVEL: info
      API_TOKEN:
    env_file: [api.env]
    ports: ["127.0.0.1:8080:8000", "9000:9000"]
    depends_on:
      db: {condition: service_healthy}
    extends: {file: common.yml, service: api}
    profiles: [prod]
    volumes: ["./data:/data"]
    healthcheck: {test: [CMD, curl, -f, http://localhost:8000/ready]}
  db:
    image: postgres:17
    environment: ["POSTGRES_DB=app"]
volumes:
  data: {}
""".strip()
    )

    result = runner.invoke(
        app,
        [
            "init",
            "--from-compose",
            str(compose),
            "--service",
            "api",
            "--output",
            str(tmp_path / "rolloutkit.yaml"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    generated = tmp_path / "rolloutkit.yaml"
    text = generated.read_text()
    document = yaml.safe_load(text)
    assert document["target"]["image"] == "registry.example/api:1.2"
    assert document["target"]["port"] == 8000
    assert document["target"]["env"]["LOG_LEVEL"] == "info"
    assert document["services"]["db"]["image"] == "postgres:17"
    assert document["contracts"]["inflight"]["request"]["path"] is None
    assert text.count("TODO:") == 3
    assert "Compose build source (not executed): api: ./api (Dockerfile.prod)" in text
    for warning in (
        "only the first container port is imported",
        "write services.db.wait_for.tcp with the port db listens on",
        "extends is not imported",
        "profiles are not imported",
        "volumes are not imported",
        "healthcheck is not imported",
        "override file docker-compose.override.yml is not imported",
    ):
        assert warning in result.output
    assert "OVERRIDE_ONLY" not in text

    config = load_config(config_path=generated, cwd=tmp_path)
    assert config.target.image == "registry.example/api:1.2"
    assert config.target.port == 8000
    assert config.target.env["API_TOKEN"] == "from-file"
    assert config.contracts.inflight is not None
    assert config.contracts.inflight.request.path is None


def test_build_without_image_warns_and_leaves_only_todo_fields(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  api:\n    build: ./api\n    ports: [8000]\n"
    )
    output = tmp_path / "generated.yaml"

    result = runner.invoke(
        app,
        [
            "init",
            "--from-compose",
            str(compose),
            "--service",
            "api",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "does not build images" in result.output
    text = output.read_text()
    assert "TODO: build and tag" in text
    assert yaml.safe_load(text)["target"]["image"] is None


def test_init_refuses_to_overwrite_existing_config(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {api: {image: api:latest, ports: [8000]}}\n")
    output = tmp_path / "rolloutkit.yaml"
    output.write_text("keep me\n")

    result = runner.invoke(
        app,
        [
            "init",
            "--from-compose",
            str(compose),
            "--service",
            "api",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 2
    assert "refusing to overwrite" in result.output
    assert output.read_text() == "keep me\n"


def test_a_dependency_declared_without_a_condition_still_asks_for_a_gate(
    tmp_path: Path,
) -> None:
    """The list form of `depends_on` is the common one, and it carried no port.

    Compose never carries a port for a dependency, so no form of `depends_on`
    can produce a gate. The shape that says `condition: service_healthy` was
    warned about and the shape that says `- db` was not, which left the
    warning on the file whose author had already thought about waiting and off
    the file whose author had not.
    """
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  api:
    image: registry.example/api:1.2
    ports: ["8000:8000"]
    depends_on:
      - db
      - cache
  db:
    image: postgres:17
  cache:
    image: redis:7-alpine
""".strip()
    )

    result = runner.invoke(
        app,
        [
            "init",
            "--from-compose",
            str(compose),
            "--service",
            "api",
            "--output",
            str(tmp_path / "rolloutkit.yaml"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    for dependency in ("db", "cache"):
        assert (
            f"service api starts after {dependency} but does not wait for it; "
            f"write services.{dependency}.wait_for.tcp with the port "
            f"{dependency} listens on" in result.output
        )
    # The gate is named, never written: the port is the one thing Compose does
    # not know, so a generated `wait_for` would be a guess.
    document = yaml.safe_load((tmp_path / "rolloutkit.yaml").read_text())
    assert "wait_for" not in document["services"]["db"]
    assert "wait_for" not in document["services"]["cache"]
