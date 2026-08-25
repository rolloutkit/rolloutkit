"""Public CLI surface, configuration precedence, and offline commands."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from preflightkit.cli.main import app
from preflightkit.config.loader import load_config
from preflightkit.config.models import Config, Target
from preflightkit.contracts.base import ContractResult, Status
from preflightkit.engine.context import RunReport
from preflightkit.evidence.model import RunOutcome, Session
from preflightkit.reporters import junit


runner = CliRunner()


def _clear_env(monkeypatch) -> None:
    for name in (
        "PREFLIGHTKIT_CONFIG",
        "PREFLIGHTKIT_IMAGE",
        "PREFLIGHTKIT_PORT",
        "PREFLIGHTKIT_READY_URL",
        "PREFLIGHTKIT_INFLIGHT_PATH",
        "PREFLIGHTKIT_GRACE",
        "PREFLIGHTKIT_DRAIN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_one_line_configless_defaults(tmp_path: Path, monkeypatch) -> None:
    _clear_env(monkeypatch)

    config = load_config(
        image="fixture:latest",
        port=8000,
        ready_url="/ready",
        cwd=tmp_path,
    )

    assert str(config.deployment.platform) == "kubernetes"
    assert config.deployment.termination_grace_period == 30_000
    assert str(config.deployment.pre_stop.type) == "none"
    assert str(config.deployment.drain.strategy) == "none"
    assert config.contracts.inflight is not None
    assert config.contracts.inflight.request.path is None
    assert config.probe.image == "python:3.12-slim"


def test_probe_image_can_be_configured(tmp_path: Path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    path = tmp_path / "preflightkit.yaml"
    path.write_text(
        "target: {image: fixture:latest, port: 8000}\n"
        "probe: {image: python:3.13-slim}\n"
    )

    config = load_config(config_path=path)

    assert config.probe.image == "python:3.13-slim"


def test_cli_over_env_over_file_over_default(tmp_path: Path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    config_path = tmp_path / "preflightkit.yaml"
    config_path.write_text(
        """
target: {image: file:image, port: 7000}
deployment:
  termination_grace_period: 20s
  drain: {strategy: prestop}
probes:
  readiness: {path: /file}
""".strip()
    )
    monkeypatch.setenv("PREFLIGHTKIT_IMAGE", "env:image")
    monkeypatch.setenv("PREFLIGHTKIT_PORT", "7100")
    monkeypatch.setenv("PREFLIGHTKIT_READY_URL", "/env")
    monkeypatch.setenv("PREFLIGHTKIT_GRACE", "25s")
    monkeypatch.setenv("PREFLIGHTKIT_DRAIN", "none")

    env_config = load_config(cwd=tmp_path)
    cli_config = load_config(
        image="cli:image",
        port=7200,
        ready_url="/cli",
        grace="35s",
        drain="prestop",
        cwd=tmp_path,
    )

    assert (env_config.target.image, env_config.target.port) == ("env:image", 7100)
    assert env_config.probes.readiness.path == "/env"
    assert env_config.deployment.termination_grace_period == 25_000
    assert str(env_config.deployment.drain.strategy) == "none"
    assert (cli_config.target.image, cli_config.target.port) == ("cli:image", 7200)
    assert cli_config.probes.readiness.path == "/cli"
    assert cli_config.deployment.termination_grace_period == 35_000
    assert str(cli_config.deployment.drain.strategy) == "prestop"


def test_inflight_path_builds_the_primary_contract(tmp_path: Path, monkeypatch) -> None:
    _clear_env(monkeypatch)

    config = load_config(
        image="fixture:latest",
        port=8000,
        inflight_path="/slow",
        cwd=tmp_path,
    )

    assert config.contracts.inflight is not None
    assert config.contracts.inflight.request.path == "/slow"
    assert config.contracts.inflight.request.expected_duration == 5_000


def test_validate_bad_config_is_exit_2_and_never_needs_docker(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("target: {image: fixture, port: nope}\n")

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 2
    assert "config error" in result.output
    assert "target.port" in result.output


@pytest.mark.parametrize("contract_id", [f"SP{number:03}" for number in range(1, 7)])
def test_explain_documents_every_contract(contract_id: str) -> None:
    result = runner.invoke(app, ["explain", contract_id])

    assert result.exit_code == 0
    assert "Measures:" in result.output
    assert "Preconditions:" in result.output
    assert "Verdicts:" in result.output
    assert "Why it matters:" in result.output


def test_explain_sp004_names_all_drain_strategies() -> None:
    result = runner.invoke(app, ["explain", "SP004"])

    assert "prestop:" in result.output
    assert "in_app:" in result.output
    assert "none:" in result.output


def test_list_contracts_names_required_and_every_strategy() -> None:
    result = runner.invoke(app, ["list-contracts"])

    assert result.exit_code == 0
    for contract_id in ("SP001", "SP002", "SP003", "SP004", "SP005", "SP006"):
        assert contract_id in result.output
    assert result.output.count("yes") == 6
    assert result.output.count("prestop, in_app, none") == 6


def test_junit_is_parseable_and_maps_contract_statuses() -> None:
    config = Config(target=Target(image="fixture:latest", port=8000))
    report = RunReport(config=config)
    results = [
        ContractResult("SP001", "startup", Status.PASS, "ready"),
        ContractResult(
            "SP002", "readiness", Status.FAIL, "flapped", evidence={"body": "x" * 5000}
        ),
        ContractResult("SP004", "drain", Status.INCONCLUSIVE, "proxy"),
        ContractResult("SP005", "inflight", Status.SKIP, "not configured"),
    ]
    session = Session(
        run_id="pfk_test",
        image="fixture:latest",
        runs=[RunOutcome(report=report, results=results)],
    )

    root = ET.fromstring(junit.dump(session, "test"))

    assert root.tag == "testsuite"
    assert root.attrib == {
        **root.attrib,
        "tests": "4",
        "failures": "1",
        "errors": "0",
        "skipped": "2",
    }
    cases = root.findall("testcase")
    assert cases[1].find("failure") is not None
    assert len(cases[1].find("failure").text or "") <= 2_000
    assert cases[2].find("skipped").attrib["message"].startswith("INCONCLUSIVE")
    assert cases[3].find("skipped").attrib["message"].startswith("SKIP")
