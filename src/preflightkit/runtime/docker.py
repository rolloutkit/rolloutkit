"""Docker Engine API over the unix socket.

Chosen over docker-py because docker-py is synchronous: every call would have to
cross `anyio.to_thread`, adding scheduling latency to the exact millisecond
measurements this tool exists to report. Shelling out to the CLI adds process
spawn time and output parsing on top of that.
"""

from __future__ import annotations

import json
import io
import importlib.util
from pathlib import Path
import tarfile
from collections.abc import Callable, Sequence
from typing import Any

import anyio
import httpx
from anyio.abc import TaskStatus

from preflightkit.runtime.base import (
    Container,
    ContainerSpec,
    DaemonEvent,
    Network,
    Pid1Facts,
    TeardownCalibration,
    daemon_interval_ms,
    parse_proc_status,
)
from preflightkit.runtime.socket import DockerUnavailable, Endpoint, discover_endpoint

MIN_API_VERSION = (1, 41)
LABEL_OWNER = "io.preflightkit.owned"

#: Image used for the two auxiliary containers described below. Never pulled:
#: preflightkit does not fetch images on the user's behalf, and a probe that
#: silently reached the network would be a worse default than a missing
#: measurement. When it is absent the probes return None and say so.
PROBE_IMAGE = "busybox:latest"

#: Ceiling for the auxiliary containers. They run `cat` and `sleep`.
_PROBE_MEMORY_BYTES = 64 * 1024 * 1024
_PROBE_NANO_CPUS = 500_000_000
SIDECAR_CONTROL_PORT = 8765
_SIDECAR_BOOTSTRAP = r'''
import io, os, tarfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
def launch():
    time.sleep(0.05)
    os.execve("/usr/local/bin/python", ["python", "-B", "-u", "/opt/pfk_probe/sidecar_entry.py"], {"PYTHONPATH": "/opt/pfk_probe/vendor"})
class H(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        if self.path != "/load": self.send_error(404); return
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            archive.extractall("/opt")
        self.send_response(204); self.end_headers(); self.wfile.flush()
        threading.Thread(target=launch, daemon=True).start()
HTTPServer(("0.0.0.0", 8765), H).serve_forever()
'''

#: Five samples expose the host's spread without turning calibration into the
#: dominant cost of a run. Three standard deviations form the measured upper
#: noise envelope used by the deadline precondition.
_TEARDOWN_CALIBRATION_SAMPLES = 5
_TEARDOWN_STDDEV_K = 3.0

#: "Use the client default", as distinct from "no timeout at all".
#:
#: `None` cannot carry both meanings, and letting it try cost a verdict: httpx
#: spells an unlimited timeout `None`, so `wait()` passed `None` for its
#: long-poll, a `timeout is not None` check read that as "unspecified", and the
#: 30s client default applied instead. Any grace period of 30 seconds or more —
#: including the Kubernetes default — turned into an internal error rather than
#: the SP006 failure it was measuring.
_CLIENT_DEFAULT = object()


class DockerError(Exception):
    """A Docker operation failed. Maps to exit code 3."""


class DockerRuntime:
    def __init__(
        self,
        endpoint: Endpoint | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._endpoint = endpoint or discover_endpoint()
        self._progress = progress
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self._endpoint.socket_path, retries=0),
            base_url="http://docker",
            timeout=httpx.Timeout(30.0),
        )
        self._api_version: str | None = None
        self._server: dict[str, Any] = {}

    @property
    def endpoint(self) -> Endpoint:
        return self._endpoint

    @property
    def server_info(self) -> dict[str, Any]:
        return self._server

    async def __aenter__(self) -> DockerRuntime:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    # -- plumbing ---------------------------------------------------------

    def _url(self, path: str) -> str:
        prefix = f"/v{self._api_version}" if self._api_version else ""
        return f"{prefix}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None | object = _CLIENT_DEFAULT,
        **kwargs: Any,
    ) -> httpx.Response:
        if timeout is _CLIENT_DEFAULT:
            resolved: Any = httpx.USE_CLIENT_DEFAULT
        else:
            resolved = httpx.Timeout(timeout)  # type: ignore[arg-type]
        try:
            response = await self._client.request(
                method,
                self._url(path),
                timeout=resolved,
                **kwargs,
            )
        except httpx.ConnectError as exc:
            raise DockerUnavailable(
                f"cannot reach the Docker daemon at {self._endpoint.socket_path} "
                f"({self._endpoint.source}): {exc}. Is Docker running?"
            ) from exc
        except httpx.HTTPError as exc:
            raise DockerError(f"{method} {path} failed: {exc}") from exc
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, context: str) -> None:
        if response.status_code < 400:
            return
        try:
            message = response.json().get("message", response.text)
        except ValueError:
            message = response.text
        raise DockerError(f"{context}: HTTP {response.status_code}: {message}")

    async def connect(self) -> None:
        response = await self._request("GET", "/_ping", timeout=10.0)
        self._raise_for_status(response, "ping")
        version = response.headers.get("Api-Version")
        if version:
            try:
                parts = tuple(int(p) for p in version.split("."))
            except ValueError:
                parts = ()
            if parts and parts < MIN_API_VERSION:
                raise DockerUnavailable(
                    f"Docker API {version} is too old; {'.'.join(map(str, MIN_API_VERSION))}+ required"
                )
            self._api_version = version
        info = await self._request("GET", "/version", timeout=10.0)
        if info.status_code < 400:
            self._server = info.json()

    async def ping_latency_ns(self, samples: int = 5) -> list[int]:
        """Round-trip cost of a no-op call.

        This is the noise floor of every timestamp we take through the daemon.
        Reporting a measurement without reporting its jitter would be exactly the
        guessing this tool refuses to do.
        """
        from preflightkit.engine.events import now_ns

        latencies: list[int] = []
        for _ in range(samples):
            start = now_ns()
            await self._request("GET", "/_ping", timeout=5.0)
            latencies.append(now_ns() - start)
        return latencies

    # -- lifecycle --------------------------------------------------------

    async def image_exists(self, image: str) -> bool:
        response = await self._request("GET", f"/images/{image}/json")
        return response.status_code < 400

    async def ensure_image(self, image: str, *, purpose: str) -> None:
        """Pull a missing image once, with progress kept off machine output."""
        if await self.image_exists(image):
            return
        if self._progress is not None:
            size = " (~50MB, once)" if purpose == "probe" else " (once)"
            self._progress(f"pulling {purpose} image{size}: {image}")
        try:
            completed = await anyio.run_process(
                ["docker", "pull", image],
                check=False,
            )
        except OSError as exc:
            raise DockerError(f"could not run `docker pull {image}`: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode(errors="replace").strip()
            raise DockerError(
                f"pulling {purpose} image {image!r} failed"
                + (f": {detail[-2000:]}" if detail else "")
            )

    async def create_network(self, name: str) -> Network:
        response = await self._request(
            "POST",
            "/networks/create",
            json={
                "Name": name,
                "Driver": "bridge",
                "CheckDuplicate": True,
                "Labels": {LABEL_OWNER: "1"},
            },
        )
        self._raise_for_status(response, f"creating network {name}")
        return Network(id=str(response.json()["Id"]), name=name)

    async def remove_network(self, network: Network) -> None:
        response = await self._request("DELETE", f"/networks/{network.id}")
        if response.status_code not in (204, 404):
            self._raise_for_status(response, f"removing network {network.name}")

    async def start(self, spec: ContainerSpec) -> Container:
        await self.ensure_image(spec.image, purpose=spec.image_purpose)

        port_key = f"{spec.port}/tcp" if spec.port is not None else None
        host_config: dict[str, Any] = {
            # Never set Init. Docker's --init makes tini PID 1, which changes
            # how signals reach the application — we would be measuring tini.
            "Init": False,
            "Privileged": False,
            "NetworkMode": spec.network_name or "bridge",
            "AutoRemove": False,
            "Memory": spec.memory_bytes,
            "NanoCpus": spec.nano_cpus,
        }
        if spec.publish_port and port_key is not None:
            host_config["PortBindings"] = {
                port_key: [{"HostIp": "127.0.0.1", "HostPort": ""}]
            }
        body: dict[str, Any] = {
            "Image": spec.image,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": {**spec.labels, LABEL_OWNER: "1"},
            "HostConfig": host_config,
        }
        if port_key is not None:
            body["ExposedPorts"] = {port_key: {}}
        if spec.network_name and spec.network_aliases:
            body["NetworkingConfig"] = {
                "EndpointsConfig": {
                    spec.network_name: {"Aliases": list(spec.network_aliases)}
                }
            }
        if spec.command is not None:
            body["Cmd"] = spec.command

        params = {"name": spec.name} if spec.name else {}
        created = await self._request("POST", "/containers/create", params=params, json=body)
        self._raise_for_status(created, f"creating container from {spec.image}")
        container_id = created.json()["Id"]

        started = await self._request("POST", f"/containers/{container_id}/start")
        if started.status_code >= 400:
            await self._request("DELETE", f"/containers/{container_id}", params={"force": "1"})
            self._raise_for_status(started, f"starting container from {spec.image}")

        details = await self._request("GET", f"/containers/{container_id}/json")
        if details.status_code >= 400:
            await self._request(
                "DELETE", f"/containers/{container_id}", params={"force": "1"}
            )
            self._raise_for_status(details, "inspecting container")
        info = details.json()
        network_settings = info.get("NetworkSettings") or {}
        networks = network_settings.get("Networks") or {}
        network_info = networks.get(spec.network_name or "bridge") or {}
        container_ip = str(network_info.get("IPAddress") or "")
        if not container_ip:
            await self._request(
                "DELETE", f"/containers/{container_id}", params={"force": "1"}
            )
            raise DockerError(
                f"container received no IP on {spec.network_name or 'bridge'}"
            )

        published_port: int | None = None
        if spec.publish_port and port_key is not None:
            mapping = (network_settings.get("Ports") or {}).get(port_key)
            if not mapping:
                await self._request(
                    "DELETE", f"/containers/{container_id}", params={"force": "1"}
                )
                raise DockerError(f"container exposed no host binding for {port_key}")
            host = mapping[0].get("HostIp") or "127.0.0.1"
            host_port = int(mapping[0]["HostPort"])
            published_port = host_port
        else:
            host = container_ip
            host_port = spec.port or 0
        return Container(
            id=container_id,
            name=info.get("Name", "").lstrip("/"),
            host=host,
            host_port=host_port,
            container_ip=container_ip,
            published_port=published_port,
        )

    async def start_traffic_probe(
        self, *, image: str, network_name: str, name: str
    ) -> Container:
        """Start the restricted traffic sidecar from a generic Python image.

        A standard-library bootstrap receives the executable and its pure-Python
        dependencies into a bounded tmpfs. No registry-hosted preflightkit image,
        Docker socket, or host filesystem mount is involved.
        """
        await self.ensure_image(image, purpose="probe")
        port_key = f"{SIDECAR_CONTROL_PORT}/tcp"
        body = _traffic_probe_body(image, network_name, name)
        created = await self._request(
            "POST", "/containers/create", params={"name": name}, json=body
        )
        self._raise_for_status(created, f"creating traffic probe from {image}")
        container_id = str(created.json()["Id"])
        try:
            started = await self._request("POST", f"/containers/{container_id}/start")
            self._raise_for_status(started, f"starting traffic probe from {image}")
            details = await self._request("GET", f"/containers/{container_id}/json")
            self._raise_for_status(details, "inspecting traffic probe")
            info = details.json()
            network_info = (
                (info.get("NetworkSettings") or {}).get("Networks") or {}
            ).get(network_name) or {}
            mapping = (
                (info.get("NetworkSettings") or {}).get("Ports") or {}
            ).get(port_key)
            if not network_info.get("IPAddress") or not mapping:
                raise DockerError("traffic probe did not attach to the run network")
            container = Container(
                id=container_id,
                name=info.get("Name", "").lstrip("/"),
                host=mapping[0].get("HostIp") or "127.0.0.1",
                host_port=int(mapping[0]["HostPort"]),
                container_ip=str(network_info["IPAddress"]),
                published_port=int(mapping[0]["HostPort"]),
            )
            archive = _traffic_probe_archive()
            async with httpx.AsyncClient(
                base_url=f"http://{container.host}:{container.host_port}", timeout=15
            ) as control:
                with anyio.fail_after(15):
                    while True:
                        try:
                            copied = await control.post(
                                "/load",
                                content=archive,
                                headers={"Content-Type": "application/x-tar"},
                            )
                            if copied.status_code == 204:
                                break
                        except httpx.HTTPError:
                            pass
                        await anyio.sleep(0.05)
            return container
        except BaseException as exc:
            await self._request(
                "DELETE", f"/containers/{container_id}", params={"force": "1", "v": "1"}
            )
            if isinstance(exc, TimeoutError):
                raise DockerError(
                    "traffic probe bootstrap timed out; the configured image "
                    "may not provide Python 3"
                ) from None
            raise

    async def signal(self, container: Container, sig: str) -> None:
        """Send a signal explicitly.

        Not `docker stop`: that sends its own SIGKILL on its own timer, which
        would make SP006 a measurement of our tooling rather than of the app.
        """
        response = await self._request(
            "POST", f"/containers/{container.id}/kill", params={"signal": sig}
        )
        self._raise_for_status(response, f"sending {sig}")

    async def wait(self, container: Container, timeout_ms: int) -> int | None:
        """Block until the container stops. Returns None if the timeout wins.

        The bound is `anyio.fail_after`, not httpx: the request itself is a
        long-poll with no timeout of its own, so the only clock that can end this
        wait is the shutdown budget the caller passed in.
        """
        try:
            with anyio.fail_after(timeout_ms / 1000):
                response = await self._request(
                    "POST",
                    f"/containers/{container.id}/wait",
                    params={"condition": "not-running"},
                    timeout=None,
                )
                self._raise_for_status(response, "waiting for container exit")
                return int(response.json()["StatusCode"])
        except TimeoutError:
            return None

    # -- observation ------------------------------------------------------

    async def watch_events(
        self,
        container_id: str,
        actions: Sequence[str],
        on_event: Callable[[DaemonEvent], None],
        *,
        task_status: TaskStatus[bool] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        """Follow the daemon's own event stream for one container until cancelled.

        Started with `task_group.start` so the caller knows the stream is open
        before it sends anything: a signal sent first would race the subscription
        and lose the frame that dates it.

        Reports False through the task status rather than raising when the stream
        cannot be opened. Every measurement here is a refinement of one we can
        already make without it, so a daemon that will not stream events costs
        precision, not a run.
        """
        from preflightkit.engine.events import now_ns

        params = {
            "filters": json.dumps(
                {"container": [container_id], "event": list(actions)}
            )
        }
        announced = False
        try:
            async with self._client.stream(
                "GET",
                self._url("/events"),
                params=params,
                timeout=httpx.Timeout(None),
            ) as response:
                if response.status_code >= 400:
                    task_status.started(False)
                    return
                task_status.started(True)
                announced = True
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                        on_event(
                            DaemonEvent(
                                action=str(frame.get("Action", "")),
                                daemon_ns=int(frame["timeNano"]),
                                observed_ns=now_ns(),
                            )
                        )
                    except (ValueError, KeyError, TypeError):
                        continue
        except httpx.HTTPError:
            if not announced:
                task_status.started(False)

    async def _create_probe(
        self, cmd: list[str], host_config: dict[str, Any]
    ) -> str | None:
        """An auxiliary container of our own, or None if we cannot have one.

        Never pulls, unlike `start()`, which announces a pull and performs it.
        Reaching the network to complete a diagnostic would make an offline run
        behave differently from an online one without saying so; a missing image
        here degrades to the fallback path, and the report names it.
        """
        if not await self.image_exists(PROBE_IMAGE):
            return None
        body: dict[str, Any] = {
            "Image": PROBE_IMAGE,
            "Cmd": cmd,
            "Labels": {LABEL_OWNER: "1"},
            "ExposedPorts": {
                key: {} for key in (host_config.get("PortBindings") or {})
            },
            "HostConfig": {
                "Init": False,
                "Privileged": False,
                "AutoRemove": False,
                "ReadonlyRootfs": True,
                "Memory": _PROBE_MEMORY_BYTES,
                "NanoCpus": _PROBE_NANO_CPUS,
                **host_config,
            },
        }
        created = await self._request("POST", "/containers/create", json=body)
        if created.status_code >= 400:
            return None
        return str(created.json()["Id"])

    async def _discard_probe(self, probe_id: str) -> None:
        # Shielded: this runs from a `finally` that may already be unwinding a
        # cancellation, and a leaked container is worse than a slow exit.
        with anyio.CancelScope(shield=True):
            try:
                await self._request(
                    "DELETE", f"/containers/{probe_id}", params={"force": "1", "v": "1"}
                )
            except (DockerError, DockerUnavailable):
                pass

    async def probe_pid1(
        self, container: Container, *, timeout_ms: int = 15_000
    ) -> Pid1Facts | None:
        """Read /proc/1/status from inside the target's PID namespace.

        The archive endpoint (`docker cp`) cannot do this: it reads the layered
        filesystem, and /proc is not in it. A container sharing the namespace
        can, and sees the target's PID 1 as its own PID 1.

        Returns None rather than raising for every reason it might not work — no
        probe image locally, a daemon that refuses PidMode, an unparseable file.
        The verdict does not depend on it; the diagnosis does.
        """
        probe_id = await self._create_probe(
            ["cat", "/proc/1/status"],
            {
                "PidMode": f"container:{container.id}",
                "NetworkMode": "none",
            },
        )
        if probe_id is None:
            return None
        try:
            started = await self._request("POST", f"/containers/{probe_id}/start")
            if started.status_code >= 400:
                return None
            with anyio.move_on_after(timeout_ms / 1000):
                await self._request(
                    "POST",
                    f"/containers/{probe_id}/wait",
                    params={"condition": "not-running"},
                    timeout=None,
                )
            response = await self._request(
                "GET",
                f"/containers/{probe_id}/logs",
                params={"stdout": "1", "stderr": "1"},
            )
            if response.status_code >= 400:
                return None
            return parse_proc_status(_demux(response.content))
        except (DockerError, DockerUnavailable):
            return None
        finally:
            await self._discard_probe(probe_id)

    async def measure_teardown_floor(
        self,
        *,
        port: int,
        network_name: str,
        publish_port: bool,
        timeout_ms: int = 15_000,
    ) -> TeardownCalibration | None:
        """Measure the daemon's teardown distribution in the target's shape.

        A `sleep` is started and SIGKILLed, and the interval between the daemon's
        own `kill` and `die` frames is measured. SIGKILL cannot be caught,
        blocked or delayed — the kernel destroys the process on delivery — so
        every millisecond in this number is the daemon noticing and reporting,
        not an application shutting down.

        It exists because that cost is not small, and it is almost entirely
        network plumbing rather than process teardown. Measured three times each
        on Docker Desktop with the same `sleep` and the same SIGKILL:

            no network                12.3 - 12.8ms
            bridge, no published port 43.1 - 52.0ms
            bridge + published port   82.8 - 94.1ms

        The probe joins the target's run-scoped bridge and only publishes a port
        when the target did. Linux direct-IP and Docker Desktop fallback runs
        therefore calibrate the distinct network shapes they actually measure.

        Five samples produce a median floor and sample standard deviation. The
        resolution threshold is `median + 3 * stddev`: an observed, host-local
        upper noise envelope rather than a fixed multiple of one noisy sample.
        The calibration is reported, never subtracted. A measurement minus an
        estimate is a figure nothing observed.
        """
        samples: list[float] = []
        for _ in range(_TEARDOWN_CALIBRATION_SAMPLES):
            sample = await self._measure_teardown_once(
                port=port,
                network_name=network_name,
                publish_port=publish_port,
                timeout_ms=timeout_ms,
            )
            if sample is None:
                return None
            samples.append(sample)
        return TeardownCalibration(tuple(samples), stddev_k=_TEARDOWN_STDDEV_K)

    async def _measure_teardown_once(
        self,
        *,
        port: int,
        network_name: str,
        publish_port: bool,
        timeout_ms: int,
    ) -> float | None:
        host_config: dict[str, Any] = {"NetworkMode": network_name}
        if publish_port:
            host_config["PortBindings"] = {
                f"{port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]
            }
        probe_id = await self._create_probe(
            ["sleep", "30"],
            host_config,
        )
        if probe_id is None:
            return None
        frames: list[DaemonEvent] = []
        try:
            started = await self._request("POST", f"/containers/{probe_id}/start")
            if started.status_code >= 400:
                return None
            async with anyio.create_task_group() as tg:
                streaming = await tg.start(
                    self.watch_events, probe_id, ("kill", "die"), frames.append
                )
                if not streaming:
                    tg.cancel_scope.cancel()
                    return None
                await self._request(
                    "POST", f"/containers/{probe_id}/kill", params={"signal": "SIGKILL"}
                )
                with anyio.move_on_after(timeout_ms / 1000):
                    while not any(f.action == "die" for f in frames):
                        await anyio.sleep(0.005)
                tg.cancel_scope.cancel()
        except (DockerError, DockerUnavailable):
            return None
        finally:
            await self._discard_probe(probe_id)
        return daemon_interval_ms(frames, "kill", "die")

    async def logs(self, container: Container, tail: int = 50) -> str:
        response = await self._request(
            "GET",
            f"/containers/{container.id}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
        )
        if response.status_code >= 400:
            return ""
        return _demux(response.content)

    async def inspect(self, container: Container) -> dict[str, Any]:
        response = await self._request("GET", f"/containers/{container.id}/json")
        self._raise_for_status(response, "inspecting container")
        return response.json()

    async def inspect_image(self, image: str) -> dict[str, Any]:
        response = await self._request("GET", f"/images/{image}/json")
        self._raise_for_status(response, f"inspecting image {image}")
        return response.json()

    async def remove(self, container: Container) -> None:
        await self._request(
            "DELETE",
            f"/containers/{container.id}",
            params={"force": "1", "v": "1"},
        )


def _demux(payload: bytes) -> str:
    """Un-frame the Docker multiplexed log stream.

    Without a TTY each chunk is prefixed with an 8-byte header. A container that
    *does* allocate a TTY sends raw bytes; the heuristic below tells them apart.
    """
    out: list[str] = []
    offset = 0
    while offset + 8 <= len(payload):
        stream_type = payload[offset]
        if stream_type not in (0, 1, 2):
            return payload.decode("utf-8", "replace")
        size = int.from_bytes(payload[offset + 4 : offset + 8], "big")
        chunk = payload[offset + 8 : offset + 8 + size]
        out.append(chunk.decode("utf-8", "replace"))
        offset += 8 + size
    if offset == 0:
        return payload.decode("utf-8", "replace")
    return "".join(out)


def json_lines(payload: bytes) -> list[dict[str, Any]]:
    result = []
    for line in payload.splitlines():
        if line.strip():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return result


def _traffic_probe_archive() -> bytes:
    """Build the runtime payload copied into the generic probe image."""
    root = Path(__file__).resolve().parents[1]
    files = (
        root / "__init__.py",
        root / "runtime" / "sidecar_entry.py",
        root / "engine" / "__init__.py",
        root / "engine" / "events.py",
        root / "engine" / "bus.py",
        root / "traffic" / "__init__.py",
        root / "traffic" / "accept_probe.py",
        root / "traffic" / "client.py",
        root / "traffic" / "generator.py",
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for source in files:
            relative = source.relative_to(root)
            destination = (
                Path("pfk_probe/sidecar_entry.py")
                if relative == Path("runtime/sidecar_entry.py")
                else Path("pfk_probe/vendor/preflightkit") / relative
            )
            archive.add(source, arcname=str(destination), recursive=False)
        for package in ("anyio", "sniffio", "idna"):
            spec = importlib.util.find_spec(package)
            if spec is None or not spec.submodule_search_locations:
                raise DockerError(f"cannot package traffic probe dependency {package}")
            source = Path(next(iter(spec.submodule_search_locations)))

            def filter_cache(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                return None if "__pycache__" in Path(info.name).parts else info

            archive.add(
                source,
                arcname=f"pfk_probe/vendor/{package}",
                filter=filter_cache,
            )
        typing_spec = importlib.util.find_spec("typing_extensions")
        if typing_spec is None or typing_spec.origin is None:
            raise DockerError("cannot package traffic probe dependency typing_extensions")
        archive.add(
            Path(typing_spec.origin),
            arcname="pfk_probe/vendor/typing_extensions.py",
            recursive=False,
        )
    return output.getvalue()


def _traffic_probe_body(image: str, network_name: str, name: str) -> dict[str, Any]:
    """Docker create payload, kept inspectable for security regression tests."""
    port_key = f"{SIDECAR_CONTROL_PORT}/tcp"
    return {
        "Image": image,
        "Cmd": ["python", "-B", "-u", "-c", _SIDECAR_BOOTSTRAP],
        "Labels": {LABEL_OWNER: "1"},
        "ExposedPorts": {port_key: {}},
        "HostConfig": {
            "Init": False,
            "Privileged": False,
            "ReadonlyRootfs": True,
            "Tmpfs": {
                "/opt/pfk_probe": "rw,nosuid,nodev,noexec,size=16m,mode=0755"
            },
            "NetworkMode": network_name,
            "AutoRemove": False,
            "Memory": 128 * 1024 * 1024,
            "NanoCpus": _PROBE_NANO_CPUS,
            "PidsLimit": 128,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {
                port_key: [{"HostIp": "127.0.0.1", "HostPort": ""}]
            },
        },
        "NetworkingConfig": {
            "EndpointsConfig": {
                network_name: {"Aliases": ["preflightkit-probe"]}
            }
        },
    }
