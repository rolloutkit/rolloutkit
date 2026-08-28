"""Public CLI surface, configuration precedence, and offline commands."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from rolloutkit.cli.main import app
from rolloutkit import __version__
from rolloutkit.config.loader import ConfigError, load_config
from rolloutkit.config.models import Config, Target
from rolloutkit.contracts import ALL_CONTRACTS
from rolloutkit.contracts.base import ContractResult, Status
from rolloutkit.engine.context import RunReport
from rolloutkit.evidence.model import RunOutcome, Session
from rolloutkit.reporters import json_out, junit


runner = CliRunner()


def _clear_env(monkeypatch) -> None:
    for name in (
        "ROLLOUTKIT_CONFIG",
        "ROLLOUTKIT_IMAGE",
        "ROLLOUTKIT_PORT",
        "ROLLOUTKIT_READY_URL",
        "ROLLOUTKIT_INFLIGHT_PATH",
        "ROLLOUTKIT_GRACE",
        "ROLLOUTKIT_DRAIN",
        "ROLLOUTKIT_ENV",
        "ROLLOUTKIT_ENV_FILE",
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
    path = tmp_path / "rolloutkit.yaml"
    path.write_text(
        "target: {image: fixture:latest, port: 8000}\n"
        "probe: {image: python:3.13-slim}\n"
    )

    config = load_config(config_path=path)

    assert config.probe.image == "python:3.13-slim"


def test_cli_over_env_over_file_over_default(tmp_path: Path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    config_path = tmp_path / "rolloutkit.yaml"
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
    monkeypatch.setenv("ROLLOUTKIT_IMAGE", "env:image")
    monkeypatch.setenv("ROLLOUTKIT_PORT", "7100")
    monkeypatch.setenv("ROLLOUTKIT_READY_URL", "/env")
    monkeypatch.setenv("ROLLOUTKIT_GRACE", "25s")
    monkeypatch.setenv("ROLLOUTKIT_DRAIN", "none")

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


def test_env_follows_cli_over_env_over_file_over_default(
    tmp_path: Path, monkeypatch
) -> None:
    """The same order every other option follows, applied to target.env."""
    config_path = tmp_path / "rolloutkit.yaml"
    config_path.write_text(
        "target:\n"
        "  image: file:image\n"
        "  port: 7000\n"
        "  env: {FROM_FILE: file, SHARED: file}\n"
    )
    dotenv = tmp_path / "extra.env"
    dotenv.write_text("# a comment\nSHARED=dotenv\nFROM_DOTENV='quoted value'\n")

    _clear_env(monkeypatch)
    file_only = load_config(config_path=config_path)

    _clear_env(monkeypatch)
    monkeypatch.setenv("ROLLOUTKIT_ENV", "SHARED=process FROM_PROCESS=process")
    monkeypatch.setenv("ROLLOUTKIT_ENV_FILE", str(dotenv))
    from_process = load_config(config_path=config_path)

    # The flags are passed explicitly while the same variables still say
    # something else: the command line has to win over both.
    from_cli = load_config(
        config_path=config_path,
        env_values=["SHARED=cli"],
        env_files=[dotenv],
    )

    assert file_only.target.env == {"FROM_FILE": "file", "SHARED": "file"}
    assert from_process.target.env == {
        "FROM_FILE": "file",
        "SHARED": "process",
        "FROM_PROCESS": "process",
        "FROM_DOTENV": "quoted value",
    }
    assert from_cli.target.env == {
        "FROM_FILE": "file",
        "SHARED": "cli",
        "FROM_DOTENV": "quoted value",
    }


def test_env_file_values_are_redacted_by_name(tmp_path: Path, monkeypatch) -> None:
    """A dotenv is where credentials live, so its values have to reach redaction."""
    _clear_env(monkeypatch)
    dotenv = tmp_path / "secrets.env"
    dotenv.write_text("DATABASE_PASSWORD=hunter2\nALLOWED_HOSTS=*\n")

    config = load_config(
        image="fixture:latest", port=8000, env_files=[dotenv], cwd=tmp_path
    )

    assert "hunter2" in config.secret_values()
    assert "*" not in config.secret_values()


def test_env_can_be_referenced_from_the_config_it_is_passed_with(
    tmp_path: Path, monkeypatch
) -> None:
    _clear_env(monkeypatch)
    config_path = tmp_path / "rolloutkit.yaml"
    config_path.write_text(
        "target: {image: 'fixture:${TAG}', port: 8000, env: {TAG: '${TAG}'}}\n"
    )

    config = load_config(config_path=config_path, env_values=["TAG=v9"])

    assert config.target.image == "fixture:v9"


def test_malformed_env_names_the_argument_it_rejected(tmp_path: Path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="KEY=VALUE"):
        load_config(image="fixture:latest", port=8000, env_values=["ALLOWED_HOSTS"])
    with pytest.raises(ConfigError, match="empty variable name"):
        load_config(image="fixture:latest", port=8000, env_values=["=1"])


def test_missing_env_file_is_a_config_error_not_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    _clear_env(monkeypatch)
    result = runner.invoke(
        app,
        [
            "validate",
            "--image",
            "fixture:latest",
            "--port",
            "8000",
            "--env-file",
            str(tmp_path / "absent.env"),
        ],
    )
    assert result.exit_code == 2
    assert "env_file not found" in result.output


def test_env_reaches_the_target_through_the_command_line(
    tmp_path: Path, monkeypatch
) -> None:
    """The gap item 4 closes: a Django target needs ALLOWED_HOSTS and no config."""
    _clear_env(monkeypatch)
    result = runner.invoke(
        app,
        [
            "validate",
            "--image",
            "fixture:latest",
            "--port",
            "8000",
            "--env",
            "ALLOWED_HOSTS=*",
            "--env",
            "DEBUG=0",
        ],
    )
    assert result.exit_code == 0, result.output


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


def _in_app_config(window: str) -> str:
    return (
        "version: 1\n"
        "target: {image: fixture, port: 8000}\n"
        "deployment:\n"
        "  termination_grace_period: 30s\n"
        f"  drain: {{strategy: in_app, in_app_window: {window}}}\n"
    )


@pytest.mark.parametrize("window", ["1s", "500ms", "50ms"])
def test_an_unresolvable_drain_window_is_rejected_before_docker(
    tmp_path: Path, window: str
) -> None:
    """The probe cannot resolve it, and nothing has to run to know that.

    Both sides of this comparison are fixed before the run: a declared window
    against the probe's own sampling interval. SP004 used to answer it as an
    INCONCLUSIVE verdict, which meant pulling an image, starting it, signalling
    it and waiting for the exit first.
    """
    path = tmp_path / "narrow.yaml"
    path.write_text(_in_app_config(window))

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 2
    assert "config error" in result.output
    assert "in_app_window" in result.output
    assert "1000ms" in result.output, "the message must name the floor it failed"
    assert "prestop" in result.output, "the message must name the way out"


def test_an_unresolvable_drain_window_stops_test_too(tmp_path: Path) -> None:
    """Rejecting it in `validate` alone would still let `test` start a container."""
    path = tmp_path / "narrow.yaml"
    path.write_text(_in_app_config("1s"))

    result = runner.invoke(app, ["test", "--config", str(path)])

    assert result.exit_code == 2
    assert "in_app_window" in result.output


def test_a_resolvable_drain_window_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "wide.yaml"
    path.write_text(_in_app_config("5s"))

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 0
    assert "configuration valid" in result.output


def test_the_window_floor_binds_only_the_strategy_that_measures_it(
    tmp_path: Path,
) -> None:
    """prestop and none never read listener timing, so the floor cannot apply."""
    for strategy in ("prestop", "none"):
        path = tmp_path / f"{strategy}.yaml"
        path.write_text(
            "version: 1\n"
            "target: {image: fixture, port: 8000}\n"
            "deployment:\n"
            "  termination_grace_period: 30s\n"
            "  pre_stop: {type: sleep, duration: 5s}\n"
            f"  drain: {{strategy: {strategy}, in_app_window: 1s}}\n"
        )

        result = runner.invoke(app, ["validate", "--config", str(path)])

        assert result.exit_code == 0, f"{strategy}: {result.output}"


def _budget_config(budget: str, wall: str) -> str:
    return (
        "version: 1\n"
        "target: {image: fixture, port: 8000}\n"
        f"contracts: {{startup: {{budget: {budget}}}}}\n"
        f"timeouts: {{startup: {wall}}}\n"
    )


@pytest.mark.parametrize(
    ("budget", "wall"),
    [("90s", "90s"), ("120s", "90s"), ("31s", "30s")],
)
def test_a_startup_budget_past_the_timeout_is_rejected(
    tmp_path: Path, budget: str, wall: str
) -> None:
    """SP001's over_budget branch has to stay reachable.

    The budget only warns; the timeout aborts the run with exit 3 and nothing
    measured. Ordered the wrong way round, every container slow enough to
    exceed the budget is killed by the timeout first, so the warning is dead
    code and the run reports an infrastructure error in its place.
    """
    path = tmp_path / "budget.yaml"
    path.write_text(_budget_config(budget, wall))

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 2
    assert "config error" in result.output
    assert "contracts.startup.budget" in result.output
    assert "timeouts.startup" in result.output


def test_a_startup_budget_past_the_timeout_stops_test_too(tmp_path: Path) -> None:
    """Rejecting it in `validate` alone would still let `test` start a container."""
    path = tmp_path / "budget.yaml"
    path.write_text(_budget_config("90s", "90s"))

    result = runner.invoke(app, ["test", "--config", str(path)])

    assert result.exit_code == 2
    assert "contracts.startup.budget" in result.output


def test_the_shipped_startup_defaults_leave_the_budget_reachable() -> None:
    """The defaults are themselves an instance of the rule."""
    config = Config(target=Target(image="fixture", port=8000))

    assert config.contracts.startup.budget < config.timeouts.startup


def _overall_config(value: str = "120s") -> str:
    return (
        "version: 1\n"
        "target: {image: fixture, port: 8000}\n"
        f"timeouts: {{startup: 90s, shutdown: 45s, overall: {value}}}\n"
    )


def test_a_removed_setting_is_named_rather_than_called_a_typo(tmp_path: Path) -> None:
    """`timeouts.overall` was in the schema and bounded nothing; now it is gone.

    `extra="forbid"` already refuses it, but as "Extra inputs are not
    permitted", which sends a reader looking for the spelling mistake they did
    not make. A setting that shipped and was taken out again has to say so.
    """
    path = tmp_path / "overall.yaml"
    path.write_text(_overall_config())

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 2
    assert "config error" in result.output
    assert "timeouts.overall" in result.output
    assert __version__ in result.output, "the message must name the version"
    assert "docs/v0.2.md" in result.output, "the message must name where it went"
    assert "Extra inputs are not permitted" not in result.output


def test_a_removed_setting_stops_test_too(tmp_path: Path) -> None:
    """Rejecting it in `validate` alone would still let `test` start a container."""
    path = tmp_path / "overall.yaml"
    path.write_text(_overall_config("240s"))

    result = runner.invoke(app, ["test", "--config", str(path)])

    assert result.exit_code == 2
    assert "timeouts.overall" in result.output


def test_the_removed_setting_is_out_of_the_schema(tmp_path: Path) -> None:
    """The rejection is the message, not the mechanism.

    If the field came back to `Timeouts` the loader would go on printing a
    removal notice for a setting that exists, which is worse than either state
    on its own.
    """
    assert "overall" not in Config.model_fields["timeouts"].annotation.model_fields


def test_an_actual_typo_still_reads_as_a_typo(tmp_path: Path) -> None:
    """The removal notice must not become the answer to every unknown key."""
    path = tmp_path / "typo.yaml"
    path.write_text(
        "version: 1\n"
        "target: {image: fixture, port: 8000}\n"
        "timeouts: {startup: 90s, shutdwn: 45s}\n"
    )

    result = runner.invoke(app, ["validate", "--config", str(path)])

    assert result.exit_code == 2
    assert "shutdwn" in result.output
    assert "docs/v0.2.md" not in result.output


@pytest.mark.parametrize("contract_id", [f"SP{number:03}" for number in range(1, 7)])
def test_explain_documents_every_contract(contract_id: str) -> None:
    result = runner.invoke(app, ["explain", contract_id])

    assert result.exit_code == 0
    assert "Measures:" in result.output
    assert "Preconditions:" in result.output
    assert "Verdicts (branch" in result.output
    assert "Why it matters:" in result.output
    assert "First step after FAIL:" in result.output


@pytest.mark.parametrize("contract_id", [f"SP{number:03}" for number in range(1, 7)])
def test_explain_names_the_branch_a_report_prints(contract_id: str) -> None:
    """A verdict is looked up by branch, so the branch has to be on the page."""
    contract = next(item for item in ALL_CONTRACTS if item.id == contract_id)

    result = runner.invoke(app, ["explain", contract_id])

    for branch, status in contract.BRANCHES.items():
        assert branch in result.output, f"{contract_id}: {branch} is undocumented"
        assert status.value in result.output


def test_version_flag_includes_package_version_and_commit(monkeypatch) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("ROLLOUTKIT_COMMIT", commit)

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"rolloutkit {__version__} ({commit})"


def test_explain_sp004_names_all_drain_strategies() -> None:
    result = runner.invoke(app, ["explain", "SP004"])

    assert "prestop:" in result.output
    assert "in_app:" in result.output
    assert "none:" in result.output


def test_explain_sp004_groups_verdicts_under_the_strategy_that_reaches_them() -> None:
    """Reading the whole table as if it applied to you is the common mistake."""
    output = runner.invoke(app, ["explain", "SP004"]).output
    section = output.index("Verdicts (branch, by drain strategy):")
    prestop = output.index("  prestop:", section)
    in_app = output.index("  in_app:", prestop)
    none = output.index("  none:", in_app)
    shared = output.index("  any strategy:", none)

    assert prestop < output.index("prestop_not_applicable", prestop) < in_app
    assert in_app < output.index("in_app_listener_closed_early", in_app) < none
    assert none < output.index("none_uncovered", none) < shared
    assert shared < output.index("budget_below_teardown_floor", shared)


def test_explain_sp005_separates_opt_out_from_unmeasurable() -> None:
    """SKIP, INCONCLUSIVE and ERROR mean three different things here."""
    output = runner.invoke(app, ["explain", "SP005"]).output

    assert "SKIP         disabled" in output
    assert "ERROR        nothing_in_flight" in output
    assert "INCONCLUSIVE baseline_not_2xx" in output
    assert "required contract" in output


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
        run_id="rk_test",
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


def test_json_reports_per_run_and_aggregate_phase_durations() -> None:
    config = Config(target=Target(image="fixture:latest", port=8000))
    first = RunReport(config=config)
    second = RunReport(config=config)
    first.phase_durations_ms["baseline"] = 12.25
    second.phase_durations_ms["baseline"] = 7.75
    first.phase_durations_ms["teardown"] = 2.5
    second.phase_durations_ms["teardown"] = 3.5
    session = Session(
        run_id="rk_phases",
        image="fixture:latest",
        runs=[RunOutcome(report=first), RunOutcome(report=second)],
    )

    document = json_out.build(session, __version__)

    assert document["phase_durations_ms"]["baseline"] == 20.0
    assert document["phase_durations_ms"]["teardown"] == 6.0
    assert document["runs"][0]["phase_durations_ms"]["baseline"] == 12.25
