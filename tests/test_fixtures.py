"""Every fixture, against the verdict matrix in ``fixtures/matrix.yaml``.

This is the regression that protects the model itself. Images travel the same
code path and have to come out with different verdicts; if a change ever makes
them agree, the tool has stopped measuring and started guessing.

The CLI is invoked as a subprocess rather than the engine being called
in-process, so the exit code — the thing CI actually reacts to — is covered too.

Statuses are checked, and so is the branch that produced each one. A contract
that reaches the right verdict by the wrong route is a defect that a
status-only matrix cannot see; `tests/test_coverage.py` guarantees every branch
has a row here to be seen in.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

pytestmark = pytest.mark.docker

#: Blocking statuses under `--fail-on error`. WARN is absent: a slow shutdown is
#: worth reporting, never worth failing a pipeline over.
BLOCKING = {"FAIL", "ERROR", "INCONCLUSIVE", "SKIP"}


def _matrix() -> dict:
    return yaml.safe_load((FIXTURES / "matrix.yaml").read_text())


def _fixtures() -> list[dict]:
    return _matrix()["fixtures"]


def _docker_available() -> bool:
    binary = shutil.which("docker")
    if binary is None:
        return False
    probe = subprocess.run(
        [binary, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


def _cli() -> Path:
    return Path(sys.executable).parent / "preflightkit"


@pytest.fixture(scope="session")
def built_images() -> None:
    if not _docker_available():
        pytest.skip("no Docker daemon")
    if not _cli().exists():
        pytest.skip("preflightkit is not installed in this environment")
    for image in _matrix()["images"]:
        context = FIXTURES / image["context"]
        command = ["docker", "build", "-t", image["name"]]
        # Two images share one context and differ only in their Dockerfile —
        # the STOPSIGNAL variant would otherwise need a duplicate copy of the
        # application it is not changing.
        if "dockerfile" in image:
            command += ["-f", str(context / image["dockerfile"])]
        command.append(str(context))
        build = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if build.returncode != 0:
            pytest.fail(f"building {image['name']} failed:\n{build.stderr[-3000:]}")


@pytest.mark.parametrize("entry", _fixtures(), ids=lambda e: e["name"])
def test_fixture_matches_the_matrix(entry: dict, built_images: None) -> None:
    if entry.get("desktop_only") and platform.system() == "Linux":
        pytest.skip(
            "the fallback proxy branch exists only on Docker Desktop; Linux "
            "fallback uses the target's direct bridge address"
        )
    command = [
        str(_cli()),
        "test",
        "--config",
        str(FIXTURES / entry["config"]),
        "--format",
        "json",
        "--fail-on",
        "error",
    ]
    run = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=400,
    )
    # Exit code 3 means the experiment never happened (no daemon or the image
    # would not start). A non-2xx baseline now completes the experiment and
    # publishes SP005 INCONCLUSIVE, so it must not be excused here.
    if run.returncode == 3:
        pytest.skip(f"no verdict: {run.stderr.strip()[:300]}")

    try:
        report = json.loads(run.stdout)
    except json.JSONDecodeError:  # pragma: no cover - only on a broken build
        pytest.fail(f"no JSON report (exit {run.returncode}):\n{run.stderr[-3000:]}")

    results = {c["id"]: c for c in report["contracts"]}
    for contract_id, expectation in entry["expect"].items():
        assert contract_id in results, f"{contract_id} was not evaluated"
        result = results[contract_id]
        assert result["status"] == expectation["status"], (
            f"{contract_id}: expected {expectation['status']}, got "
            f"{result['status']} — {result['summary']}"
        )
        assert result["branch"] == expectation["branch"], (
            f"{contract_id}: right verdict ({result['status']}) from the wrong "
            f"branch — expected {expectation['branch']}, got {result['branch']} "
            f"— {result['summary']}"
        )

    if entry["name"] == "accept-then-reset-prestop":
        evidence = results["SP004"]["evidence"]
        assert evidence["accept_probe_policy"] == "stop_at_t0"
        assert evidence["attempts_started_after_t0"] == 0
        assert evidence["accept_then_reset"] == []

    if entry["name"] == "flapping-readiness":
        evidence = results["SP002"]["evidence"]
        assert evidence["n"] == 10
        assert {sample["status"] for sample in evidence["samples"]} == {200, 503}
        for sample in evidence["samples"]:
            assert {
                "status",
                "latency_ms",
                "headers",
                "body_head",
                "body_head_bytes",
            } <= sample.keys()

    # Exit gating applies to the whole report, including contracts this row is
    # not using to cover a matrix branch.
    statuses = {result["status"] for result in results.values()}
    expected_exit = 1 if BLOCKING & statuses else 0
    assert run.returncode == expected_exit


def test_contracts_are_independent(built_images: None) -> None:
    """The point of `kills-inflight`: a clean exit code proves nothing.

    SP003 sees exit 0 and passes. SP005 sees ten destroyed responses and fails.
    Any refactor that couples them will show up here first.
    """
    entry = next(e for e in _fixtures() if e["name"] == "kills-inflight")
    assert entry["expect"]["SP003"]["status"] == "PASS"
    assert entry["expect"]["SP005"]["status"] == "FAIL"


def test_allow_inconclusive_is_an_explicit_gating_escape_hatch(
    built_images: None,
) -> None:
    run = subprocess.run(
        [
            str(_cli()),
            "test",
            "--config",
            str(FIXTURES / "stdlib-http/baseline-500.yaml"),
            "--format",
            "json",
            "--fail-on",
            "error",
            "--allow-inconclusive",
        ],
        capture_output=True,
        text=True,
        timeout=400,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    sp005 = next(result for result in report["contracts"] if result["id"] == "SP005")
    assert sp005["required"] is True
    assert sp005["status"] == "INCONCLUSIVE"


def test_configless_one_line_cli_and_required_skip_gate(
    built_images: None, tmp_path: Path
) -> None:
    command = [
        str(_cli()),
        "test",
        "pfk-fixture-good",
        "--port",
        "8000",
        "--ready-url",
        "/ready",
    ]

    report_only = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert report_only.returncode == 0, report_only.stderr
    for contract_id in ("SP001", "SP002", "SP003", "SP006"):
        assert contract_id in report_only.stdout
    assert "SP004 drain-window" in report_only.stdout
    assert "WARN" in report_only.stdout
    assert "SP005 inflight-completion" in report_only.stdout
    assert "readiness p50" in report_only.stdout
    assert "jitter" in report_only.stdout
    assert "--inflight-path" in report_only.stdout

    gated = subprocess.run(
        [*command, "--fail-on", "error", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert gated.returncode == 1, gated.stderr
    document = json.loads(gated.stdout)
    assert document["profile"] == {
        "platform": "kubernetes",
        "termination_grace_period_ms": 30_000,
        "pre_stop_ms": 0,
        "shutdown_budget_ms": 30_000,
        "drain_strategy": "none",
    }
    assert document["required_unmeasured"]["contracts"][0]["id"] == "SP005"
    assert document["required_unmeasured"]["contracts"][0]["status"] == "INCONCLUSIVE"

    allowed = subprocess.run(
        [
            *command,
            "--fail-on",
            "error",
            "--allow-inconclusive",
            "--format",
            "junit",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert allowed.returncode == 0, allowed.stderr
    suite = ET.fromstring(allowed.stdout)
    assert suite.attrib["tests"] == "6"
    skipped = suite.findall("testcase/skipped")
    assert len(skipped) == 1
    sp005 = next(
        case for case in suite.findall("testcase") if case.attrib["name"].startswith("SP005")
    )
    assert sp005.find("skipped") is not None
    assert "readiness p50" in sp005.find("skipped").attrib["message"]

    measured = subprocess.run(
        [str(_cli()), "measure", *command[2:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert measured.returncode == 0, measured.stderr
    assert "SHUTDOWN TIMELINE" in measured.stdout
    assert "CONTRACTS" not in measured.stdout


def test_delayed_bind_distinguishes_linux_direct_ip_from_desktop_proxy(
    built_images: None,
) -> None:
    """Acceptance fixture, deliberately outside the verdict matrix."""
    run = subprocess.run(
        [
            str(_cli()),
            "test",
            "--config",
            str(FIXTURES / "stdlib-http/delayed-bind.yaml"),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    sp001 = next(item for item in report["contracts"] if item["id"] == "SP001")
    environment = report["environment"]

    assert environment["probe_location"] == "sidecar"
    assert environment["port_proxy_likely"] is False
    assert sp001["actual"]["tcp_open_status"] == "MEASURED"
    assert 2_500 <= sp001["actual"]["tcp_open_ms"] <= 6_000
    assert environment["traffic_endpoint"] == "target:8000"


def test_unusable_probe_image_uses_explicit_host_fallback(built_images: None) -> None:
    run = subprocess.run(
        [
            str(_cli()),
            "test",
            "--config",
            str(FIXTURES / "drain-window/host-fallback.yaml"),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = json.loads(run.stdout)
    environment = report["environment"]

    assert environment["probe_location"] == "host_fallback"
    assert "traffic probe bootstrap timed out" in environment["probe_fallback_reason"]
    sp004 = next(item for item in report["contracts"] if item["id"] == "SP004")
    assert sp004["evidence"]["probe_location"] == "host_fallback"
    assert sp004["evidence"]["probe_fallback_reason"]


def test_one_image_two_profiles(built_images: None) -> None:
    """Acceptance #3, as a fact about the matrix rather than a manual run.

    `ignores-sigterm` and `slow-shutdown` are the same image with the same
    entrypoint. The only differences are the shutdown budget and one env var, and
    the verdicts are opposite. If a change ever makes the profile stop mattering,
    the two rows will agree and this fails.
    """
    rows = {e["name"]: e for e in _fixtures()}
    assert rows["ignores-sigterm"]["image"] == rows["slow-shutdown"]["image"]
    assert rows["ignores-sigterm"]["expect"]["SP006"]["status"] == "FAIL"
    assert rows["slow-shutdown"]["expect"]["SP006"]["status"] == "WARN"
