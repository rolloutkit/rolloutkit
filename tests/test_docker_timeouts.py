"""How long a Docker request is allowed to take, and who decides.

The one regression here is not hypothetical. `wait()` long-polls the daemon
until the container stops, so it must have no timeout of its own — the shutdown
budget is the only clock that should end it. It asked for that by passing
`None`, the argument was read as "unspecified", and the 30 second client default
applied instead. Every grace period of 30 seconds or more, the Kubernetes
default included, then produced an internal error instead of a verdict.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest

from preflightkit.runtime.base import Container
from preflightkit.runtime.docker import DockerError, DockerRuntime
from preflightkit.runtime.socket import Endpoint

CONTAINER = Container(id="abc123", name="pfk-test", host="127.0.0.1", host_port=8000)


class _Recorder:
    """Stands in for the httpx client and remembers what it was told."""

    def __init__(self, body: dict[str, Any] | None = None, hang: bool = False) -> None:
        self.timeouts: list[Any] = []
        self._body = body if body is not None else {"StatusCode": 0}
        self._hang = hang

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.timeouts.append(kwargs.get("timeout"))
        if self._hang:
            await anyio.sleep(3600)
        return httpx.Response(200, json=self._body)

    async def aclose(self) -> None:
        return None


def _runtime(recorder: _Recorder) -> DockerRuntime:
    runtime = DockerRuntime(Endpoint(socket_path="/nonexistent.sock", source="test"))
    runtime._client = recorder  # type: ignore[assignment]
    return runtime


def test_wait_sends_no_timeout_of_its_own() -> None:
    recorder = _Recorder()
    runtime = _runtime(recorder)
    code = anyio.run(lambda: runtime.wait(CONTAINER, timeout_ms=120_000))
    assert code == 0
    assert len(recorder.timeouts) == 1
    timeout = recorder.timeouts[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is None, "the long-poll must not be capped by httpx"
    assert timeout.connect is None


def test_wait_is_bounded_by_the_budget_it_was_given() -> None:
    """Unlimited at the HTTP layer is not unlimited: fail_after still holds."""
    runtime = _runtime(_Recorder(hang=True))

    async def scenario() -> int | None:
        # An outer bound, so a regression here fails the suite instead of
        # hanging it for the hour the fake daemon is prepared to sleep.
        with anyio.fail_after(5):
            return await runtime.wait(CONTAINER, timeout_ms=50)

    assert anyio.run(scenario) is None


@pytest.mark.parametrize("call", ["connect", "ping_latency_ns"])
def test_short_calls_keep_their_explicit_timeout(call: str) -> None:
    """A number still means a number — the sentinel only changes the None case."""
    recorder = _Recorder(body={"ApiVersion": "1.44", "Version": "27.0", "Os": "linux"})
    runtime = _runtime(recorder)
    anyio.run(getattr(runtime, call))
    assert recorder.timeouts, "no request was made"
    assert all(t.read is not None for t in recorder.timeouts)


def test_a_task_group_wrapper_does_not_hide_what_failed() -> None:
    """The message a user gets when something unexpected escapes.

    `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` names
    neither the failure nor the place, and that is what the CLI used to print.
    """
    from preflightkit.cli.main import _describe, _leaves

    inner = ExceptionGroup("inner", [DockerError("POST /wait failed: read timeout")])
    outer = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

    assert [type(leaf) for leaf in _leaves(outer)] == [DockerError]
    described = _describe(outer)
    assert "read timeout" in described
    assert "TaskGroup" not in described


def test_a_daemon_failure_inside_a_task_group_is_still_infrastructure() -> None:
    from preflightkit.cli.main import INFRASTRUCTURE, _leaves

    group = ExceptionGroup("boom", [DockerError("daemon went away")])
    assert all(isinstance(leaf, INFRASTRUCTURE) for leaf in _leaves(group))

    mixed = ExceptionGroup("boom", [DockerError("x"), ValueError("our own bug")])
    assert not all(isinstance(leaf, INFRASTRUCTURE) for leaf in _leaves(mixed))
