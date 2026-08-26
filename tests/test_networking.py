"""Run-scoped bridge selection, dependency DNS, and lazy calibration."""

from __future__ import annotations

import statistics
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest

from preflightkit.config.loader import load_config
from preflightkit.config.models import Config, Deployment, Target
from preflightkit.engine.context import RunReport
from preflightkit.engine.lifecycle import _calibrate
from preflightkit.runtime.base import Container, ContainerSpec
from preflightkit.runtime.base import TeardownCalibration
from preflightkit.runtime.docker import (
    PROBE_IMAGE,
    DockerError,
    DockerRuntime,
    _TEARDOWN_CALIBRATION_SAMPLES,
    _TEARDOWN_STDDEV_K,
    _traffic_probe_body,
)


def _response(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {})


def test_linux_target_uses_custom_network_ip_without_publishing() -> None:
    async def scenario() -> None:
        runtime = DockerRuntime.__new__(DockerRuntime)
        bodies: list[dict[str, Any]] = []

        async def exists(_image: str) -> bool:
            return True

        async def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
            if path == "/containers/create":
                bodies.append(kwargs["json"])
                return _response(201, {"Id": "target-id"})
            if path.endswith("/start"):
                return _response(204)
            return _response(
                200,
                {
                    "Name": "/target",
                    "NetworkSettings": {
                        "Networks": {"pfk-run": {"IPAddress": "172.30.0.3"}},
                        "Ports": {"8000/tcp": None},
                    },
                },
            )

        runtime.image_exists = exists  # type: ignore[method-assign]
        runtime._request = request  # type: ignore[method-assign]
        container = await runtime.start(
            ContainerSpec(
                image="target:test",
                port=8000,
                network_name="pfk-run",
                network_aliases=("target",),
                publish_port=False,
            )
        )

        assert container.host == "172.30.0.3"
        assert container.host_port == 8000
        assert container.published_port is None
        assert bodies[0]["HostConfig"]["NetworkMode"] == "pfk-run"
        assert "PortBindings" not in bodies[0]["HostConfig"]
        assert bodies[0]["NetworkingConfig"]["EndpointsConfig"]["pfk-run"] == {
            "Aliases": ["target"]
        }

    anyio.run(scenario)


def test_desktop_target_uses_published_fallback_but_keeps_custom_bridge() -> None:
    async def scenario() -> None:
        runtime = DockerRuntime.__new__(DockerRuntime)
        body: dict[str, Any] = {}

        async def exists(_image: str) -> bool:
            return True

        async def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
            nonlocal body
            if path == "/containers/create":
                body = kwargs["json"]
                return _response(201, {"Id": "target-id"})
            if path.endswith("/start"):
                return _response(204)
            return _response(
                200,
                {
                    "Name": "/target",
                    "NetworkSettings": {
                        "Networks": {"pfk-run": {"IPAddress": "172.30.0.3"}},
                        "Ports": {
                            "8000/tcp": [
                                {"HostIp": "127.0.0.1", "HostPort": "49152"}
                            ]
                        },
                    },
                },
            )

        runtime.image_exists = exists  # type: ignore[method-assign]
        runtime._request = request  # type: ignore[method-assign]
        container = await runtime.start(
            ContainerSpec(
                image="target:test",
                port=8000,
                network_name="pfk-run",
                publish_port=True,
            )
        )

        assert (container.host, container.host_port) == ("127.0.0.1", 49152)
        assert container.container_ip == "172.30.0.3"
        assert body["HostConfig"]["NetworkMode"] == "pfk-run"
        assert "8000/tcp" in body["HostConfig"]["PortBindings"]

    anyio.run(scenario)


def test_missing_image_is_pulled_with_progress(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = DockerRuntime.__new__(DockerRuntime)
        messages: list[str] = []
        runtime._progress = messages.append

        async def absent(_image: str) -> bool:
            return False

        async def pull(command: list[str], *, check: bool):
            assert command == ["docker", "pull", "python:3.12-slim"]
            assert check is False
            return SimpleNamespace(returncode=0, stderr=b"")

        runtime.image_exists = absent  # type: ignore[method-assign]
        monkeypatch.setattr(anyio, "run_process", pull)

        await runtime.ensure_image("python:3.12-slim", purpose="probe")

        assert messages == [
            "pulling probe image (~50MB, once): python:3.12-slim"
        ]

    anyio.run(scenario)


def test_traffic_probe_is_restricted_and_has_no_host_mount() -> None:
    body = _traffic_probe_body("python:3.12-slim", "pfk-run", "probe")
    host = body["HostConfig"]

    assert host["Privileged"] is False
    assert host["NetworkMode"] == "pfk-run"
    assert host["ReadonlyRootfs"] is True
    assert host["Memory"] == 128 * 1024 * 1024
    assert host["NanoCpus"] == 500_000_000
    assert host["PidsLimit"] == 128
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert "Binds" not in host
    assert "Mounts" not in host
    assert host["NetworkMode"] != "host"


def test_container_that_exits_before_inspect_is_not_leaked() -> None:
    async def scenario() -> None:
        runtime = DockerRuntime.__new__(DockerRuntime)
        requests: list[tuple[str, str]] = []

        async def exists(_image: str) -> bool:
            return True

        async def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
            requests.append((method, path))
            if path == "/containers/create":
                return _response(201, {"Id": "fast-exit"})
            if path.endswith("/start") or method == "DELETE":
                return _response(204)
            return _response(
                200,
                {
                    "Name": "/fast-exit",
                    "NetworkSettings": {
                        "Networks": {"pfk-run": {"IPAddress": ""}}
                    },
                },
            )

        runtime.image_exists = exists  # type: ignore[method-assign]
        runtime._request = request  # type: ignore[method-assign]

        with pytest.raises(DockerError, match="received no IP"):
            await runtime.start(
                ContainerSpec(
                    image="busybox:latest",
                    port=None,
                    command=["true"],
                    network_name="pfk-run",
                )
            )
        assert ("DELETE", "/containers/fast-exit") in requests

    anyio.run(scenario)


def test_services_keep_compose_dns_names_and_env_values(tmp_path: Path) -> None:
    config_file = tmp_path / "preflightkit.yaml"
    config_file.write_text(
        """
target:
  image: service-a:test
  port: 8000
  env:
    DATABASE_URL: postgresql://database:5432/application
    CACHE_URL: cache://cache:6379/0
    OBJECT_STORE_ENDPOINT: object-store:9000
services:
  database:
    image: database-fixture:latest
  cache:
    image: cache-fixture:latest
  object-store:
    image: object-store-fixture:latest
""".strip()
    )

    config = load_config(config_path=config_file)

    assert set(config.services) == {"database", "cache", "object-store"}
    assert config.target.env["DATABASE_URL"].endswith("database:5432/application")
    assert config.target.env["CACHE_URL"] == "cache://cache:6379/0"
    assert config.target.env["OBJECT_STORE_ENDPOINT"] == "object-store:9000"
    assert "host-gateway" not in " ".join(config.target.env.values())


def test_long_budget_skips_all_five_teardown_probe_cycles() -> None:
    async def scenario() -> None:
        report = RunReport(
            config=Config(
                target=Target(image="target:test", port=8000),
                deployment=Deployment(termination_grace_period="30s"),
            )
        )
        container = Container("id", "target", "172.30.0.3", 8000)

        class Runtime:
            async def probe_pid1(self, _container: Container) -> None:
                return None

            async def measure_teardown_floor(self, **kwargs: Any) -> None:
                raise AssertionError("long budgets must not calibrate")

        await _calibrate(Runtime(), container, report, 8000, "pfk-run", False)  # type: ignore[arg-type]
        assert report.teardown_calibration is None
        assert report.teardown_calibration_status == "not_calibrated"

    anyio.run(scenario)


def test_short_budget_calibrates_in_the_targets_exact_network_shape() -> None:
    async def scenario() -> None:
        report = RunReport(
            config=Config(
                target=Target(image="target:test", port=8000),
                deployment=Deployment(termination_grace_period="2s"),
            )
        )
        container = Container("id", "target", "127.0.0.1", 49152)

        class Runtime:
            async def probe_pid1(self, _container: Container) -> None:
                return None

            async def measure_teardown_floor(self, **kwargs: Any) -> TeardownCalibration:
                assert kwargs == {
                    "port": 8000,
                    "network_name": "pfk-run",
                    "publish_port": True,
                }
                return TeardownCalibration((80.0, 82.0, 84.0, 86.0, 88.0))

        await _calibrate(Runtime(), container, report, 8000, "pfk-run", True)  # type: ignore[arg-type]
        assert report.teardown_calibration_status == "calibrated"
        assert report.teardown_floor_ms == 84.0

    anyio.run(scenario)


@pytest.mark.docker
def test_dependency_alias_resolves_on_the_runtime_created_bridge() -> None:
    async def scenario() -> None:
        async with DockerRuntime() as runtime:
            if not await runtime.image_exists("busybox:latest"):
                pytest.skip("busybox:latest is not present locally")
            network = await runtime.create_network("pfk-test-service-dns")
            containers: list[Container] = []
            try:
                dependency = await runtime.start(
                    ContainerSpec(
                        image="busybox:latest",
                        port=None,
                        command=["sleep", "30"],
                        network_name=network.name,
                        network_aliases=("db",),
                    )
                )
                containers.append(dependency)
                resolver = await runtime.start(
                    ContainerSpec(
                        image="busybox:latest",
                        port=None,
                        # A trailing dot prevents cloud-host search domains from
                        # rewriting the network alias into an unrelated FQDN.
                        command=["sh", "-c", "nslookup db. 127.0.0.11; sleep 1"],
                        network_name=network.name,
                    )
                )
                containers.append(resolver)
                assert await runtime.wait(resolver, timeout_ms=5_000) == 0
                output = await runtime.logs(resolver)
                assert "db" in output
                assert dependency.container_ip in output
            finally:
                for container in reversed(containers):
                    await runtime.remove(container)
                await runtime.remove_network(network)

    anyio.run(scenario)


@pytest.mark.docker
def test_teardown_calibration_measures_a_real_daemon_and_agrees_with_itself() -> None:
    """The one mechanism no fabricated value can stand in for.

    Both branches that consume this calibration — SP004 and SP006's
    `budget_below_teardown_floor` — are proved by handing the decision function
    known numbers, because their decision is arithmetic. That leaves the source
    of the numbers unproved, and a wrong floor is the failure that arithmetic
    cannot catch: the comparison still works, the verdict still renders, and the
    answer is confidently false. So this test does not check the decision. It
    checks that a real daemon produces samples at all, that they are in
    milliseconds rather than some other unit, and that the statistics published
    from them describe the samples they came from.

    What it deliberately does not assert is any magnitude. The floor is host
    plumbing — 12ms with no network, 83-94ms behind a published port on one
    Docker Desktop — and pinning a number measured here would make this a test
    of the machine it last ran on.
    """

    async def scenario() -> None:
        async with DockerRuntime() as runtime:
            if not await runtime.image_exists(PROBE_IMAGE):
                pytest.skip(f"{PROBE_IMAGE} is not present locally")
            network = await runtime.create_network("pfk-test-teardown-floor")
            try:
                calibration = await runtime.measure_teardown_floor(
                    port=8000, network_name=network.name, publish_port=False
                )
            finally:
                await runtime.remove_network(network)

        assert calibration is not None, (
            "the daemon was reachable but no teardown sample survived; the "
            "kill/die frames are what SP004 and SP006 resolve their budgets "
            "against, and without them both silently fall back to INCONCLUSIVE"
        )
        samples = calibration.samples_ms
        assert len(samples) == _TEARDOWN_CALIBRATION_SAMPLES

        for sample in samples:
            assert sample > 0, f"a teardown interval of {sample} is not a duration"
            # SIGKILL is delivered by the kernel and cannot be delayed, so this
            # interval is the daemon noticing and reporting. Anything near the
            # 15s timeout means the frames were not read; anything in the
            # millions means nanoseconds were published as milliseconds.
            assert sample < 15_000, f"{sample}ms is the timeout, not a measurement"

        assert calibration.floor_ms == statistics.median(samples)
        assert calibration.stddev_ms == statistics.stdev(samples)
        assert calibration.stddev_k == _TEARDOWN_STDDEV_K
        assert calibration.resolution_threshold_ms == (
            calibration.floor_ms + _TEARDOWN_STDDEV_K * calibration.stddev_ms
        )
        assert calibration.resolution_threshold_ms >= calibration.floor_ms

        published = calibration.as_dict()
        assert published["sample_count"] == _TEARDOWN_CALIBRATION_SAMPLES
        assert published["floor_statistic"] == "median"
        assert published["stddev_statistic"] == "sample"
        assert published["floor_ms"] == round(calibration.floor_ms, 3)
        assert published["resolution_threshold_ms"] == round(
            calibration.resolution_threshold_ms, 3
        )

    anyio.run(scenario)
