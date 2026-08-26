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

from preflightkit.contracts.catalog import CATALOG, Evidence
from preflightkit.contracts.inflight import MIN_JITTER_RATIO

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

pytestmark = pytest.mark.docker

#: Blocking statuses under `--fail-on error`. WARN is absent: a slow shutdown is
#: worth reporting, never worth failing a pipeline over.
BLOCKING = {"FAIL", "ERROR", "INCONCLUSIVE", "SKIP"}

#: SP005 branches that are reached by counting in-flight requests. Every other
#: branch is decided by a precondition, before the window matters.
COUNT_DEPENDENT_BRANCHES = {"all_completed", "requests_destroyed"}


def _unresolvable_branch(contract_id: str) -> str:
    """The branch this contract reaches when the measurement cannot resolve.

    Read from the catalog rather than written out here. A branch identifier
    spelled into a test is a coverage claim, and the one claim this file is not
    allowed to make is that a `decision_unit` branch was reached: the whole
    point of that classification is that the image does not decide it, so a test
    that starts a container and asserts the name is betting on the host. Taking
    the name from the registry is not the same statement — it says which branch
    the catalog currently classifies that way, and follows a rename.
    `tests/test_coverage.py` fails on the other kind.
    """
    return next(
        verdict.branch
        for verdict in CATALOG[contract_id].verdicts
        if verdict.evidence is Evidence.DECISION_UNIT
    )


def _assert_the_window_was_real(entry: dict, result: dict) -> None:
    """A counted verdict is only worth as much as the window it was counted in.

    Both checks are the tool's own published rules turned back on its fixtures.
    Without them a row that drifts toward the boundary keeps passing until the
    day a loaded runner empties its window, and then reports `nothing_in_flight`
    — an ERROR that reads as a defect in the image rather than in the fixture.
    """
    issued = result["actual"]["issued"]
    in_flight = result["actual"]["in_flight_at_sigterm"]
    assert in_flight == issued, (
        f"{entry['name']}: only {in_flight} of {issued} requests were in flight "
        f"at T0, so {result['branch']} was decided on a partly empty window. The "
        "fixture is at its margin: lengthen the endpoint it calls, or leave "
        "contracts.inflight.sigterm_after unset so the window is derived from "
        "the p50 measured on this runner."
    )

    ratio = result["evidence"]["window"]["jitter_ratio"]
    assert ratio is not None and ratio >= MIN_JITTER_RATIO, (
        f"{entry['name']}: the in-flight window is {ratio}x the measurement "
        f"jitter, under the {MIN_JITTER_RATIO}x preflightkit itself requires "
        "before it will treat the boundary as meaningful. A fixture may not "
        "assert a counted branch from a window the tool would not trust."
    )


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

    sp005 = entry["expect"].get("SP005")
    if sp005 and sp005["branch"] in COUNT_DEPENDENT_BRANCHES:
        _assert_the_window_was_real(entry, results["SP005"])

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
    """The zero-config path, without betting on which side of the fallback it lands.

    `--ready-url` and no in-flight path is the default experience, and it makes
    SP005 fall back to the readiness endpoint. Whether that fallback resolves is
    decided by the host's jitter floor rather than by the image: three runs of
    one fixture on this laptop, minutes apart, measured ratios of 4.9, 16.3 and
    4.2 against a required 10.

    This test used to assert the INCONCLUSIVE side of that, which made it the
    same coin toss the `readiness-fallback-fast` matrix row was removed for —
    and it is the second place the toss was hiding, because
    `tests/test_coverage.py` can only see rows in `fixtures/matrix.yaml`.

    What does not depend on the host is that the report agrees with its own
    measurement. Every assertion below is checked against the ratio that the
    same invocation recorded, so both outcomes are covered and neither is
    required. The wording of the declining summary belongs to
    `tests/test_preconditions.py::test_readiness_fallback_below_jitter_resolution_is_inconclusive`,
    which hands the decision known numbers instead of hoping for them.
    """
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
    if "--inflight-path" in report_only.stdout:
        # It declined. The summary has to say what it measured and what to do,
        # or the user is left holding a bare INCONCLUSIVE.
        assert "readiness p50" in report_only.stdout
        assert "jitter" in report_only.stdout
    else:
        assert "in-flight requests completed" in report_only.stdout

    gated = subprocess.run(
        [*command, "--fail-on", "error", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    document = json.loads(gated.stdout)
    assert document["profile"] == {
        "platform": "kubernetes",
        "termination_grace_period_ms": 30_000,
        "pre_stop_ms": 0,
        "shutdown_budget_ms": 30_000,
        "drain_strategy": "none",
    }

    sp005 = next(c for c in document["contracts"] if c["id"] == "SP005")
    unmeasured = document["required_unmeasured"]["contracts"]
    if sp005["status"] == "INCONCLUSIVE":
        assert sp005["branch"] == _unresolvable_branch("SP005")
        # The verdict has to follow the numbers it published, not merely be
        # allowed by them.
        precondition = sp005["evidence"]["precondition"]
        assert precondition["ratio"] < precondition["minimum_ratio"], (
            "SP005 declined the fallback while its own evidence says the window "
            f"was resolvable: {precondition}"
        )
        assert [c["id"] for c in unmeasured] == ["SP005"]
        assert unmeasured[0]["status"] == "INCONCLUSIVE"
        # A required contract that could not be measured blocks the pipeline.
        assert gated.returncode == 1, gated.stderr
    else:
        assert sp005["branch"] == "all_completed"
        window = sp005["evidence"]["window"]
        assert window["inflight_target"] == "readiness_fallback"
        ratio = window["readiness_jitter_ratio"]
        assert ratio is not None and ratio >= MIN_JITTER_RATIO, (
            "SP005 counted a fallback window without the ratio that permits it: "
            f"{window}"
        )
        assert not unmeasured, unmeasured
        # SP004 is a WARN under drain: none, and WARN does not block.
        assert gated.returncode == 0, gated.stderr

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
    sp005_case = next(
        case for case in suite.findall("testcase") if case.attrib["name"].startswith("SP005")
    )
    skipped = [
        case.attrib["name"]
        for case in suite.findall("testcase")
        if case.find("skipped") is not None
    ]
    # `--allow-inconclusive` turns an unmeasured required contract into a skip
    # instead of a failure. Whether this host produced one is the host's call;
    # that SP005 is the only contract here that can produce one is not.
    assert skipped in ([], [sp005_case.attrib["name"]]), skipped
    skip = sp005_case.find("skipped")
    if skip is not None:
        assert "readiness p50" in skip.attrib["message"]

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


def test_progress_names_each_phase_on_stderr_and_leaves_stdout_machine_clean(
    built_images: None,
) -> None:
    """Item three of the first-run problem: the warm-cache run was the silent one.

    The pull line only ever appeared on a cold cache. With the image already
    present the tool printed nothing at all until the report — which, on a slow
    endpoint, is twenty seconds of a terminal that looks hung.
    """
    run = subprocess.run(
        [
            str(_cli()),
            "test",
            "--config",
            str(FIXTURES / "stdlib-http/identical-readiness-health.yaml"),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=400,
    )
    if run.returncode == 3:
        pytest.skip(f"no verdict: {run.stderr.strip()[:300]}")

    json.loads(run.stdout)  # stdout carries the report and nothing else
    offsets = [
        run.stderr.index(phase)
        for phase in (
            "starting the traffic probe",
            "starting the target and waiting for readiness",
            "measuring the baseline",
            "sending SIGTERM",
            "removing the containers",
        )
    ]
    assert offsets == sorted(offsets), f"phases out of order:\n{run.stderr}"
