"""Wire-level classifications used by SP004."""

from __future__ import annotations

import errno

import anyio
from anyio import BrokenResourceError, EndOfStream

from rolloutkit.traffic.accept_probe import AcceptOutcome, probe_new_connection


class _Stream:
    def __init__(self, receive: bytes | BaseException = b"HTTP/1.1 200 OK\r\n") -> None:
        self.receive_result = receive

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send(self, data: bytes) -> None:
        assert b"Connection: close" in data

    async def receive(self) -> bytes:
        if isinstance(self.receive_result, BaseException):
            raise self.receive_result
        return self.receive_result


def test_response_status_is_irrelevant_to_an_accepted_connection(monkeypatch) -> None:
    async def connect(*args: object, **kwargs: object) -> _Stream:
        return _Stream(b"HTTP/1.1 503 Service Unavailable\r\n")

    monkeypatch.setattr(anyio, "connect_tcp", connect)
    result = anyio.run(
        lambda: probe_new_connection(host="127.0.0.1", port=8000, path="/ready")
    )
    assert result.outcome is AcceptOutcome.RESPONSE
    assert result.accepted


def test_connection_refused_is_clean_terminal_rejection(monkeypatch) -> None:
    async def connect(*args: object, **kwargs: object) -> _Stream:
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(anyio, "connect_tcp", connect)
    result = anyio.run(
        lambda: probe_new_connection(host="127.0.0.1", port=8000, path="/ready")
    )
    assert result.outcome is AcceptOutcome.REFUSED
    assert not result.accepted


def test_reset_after_handshake_is_an_accepted_obligation(monkeypatch) -> None:
    async def connect(*args: object, **kwargs: object) -> _Stream:
        return _Stream(BrokenResourceError())

    monkeypatch.setattr(anyio, "connect_tcp", connect)
    result = anyio.run(
        lambda: probe_new_connection(host="127.0.0.1", port=8000, path="/ready")
    )
    assert result.outcome is AcceptOutcome.RESET
    assert result.connected_ns is not None


def test_clean_fin_without_response_is_not_mislabeled_reset(monkeypatch) -> None:
    async def connect(*args: object, **kwargs: object) -> _Stream:
        return _Stream(EndOfStream())

    monkeypatch.setattr(anyio, "connect_tcp", connect)
    result = anyio.run(
        lambda: probe_new_connection(host="127.0.0.1", port=8000, path="/ready")
    )
    assert result.outcome is AcceptOutcome.CLOSED_WITHOUT_RESPONSE
    assert result.accepted
