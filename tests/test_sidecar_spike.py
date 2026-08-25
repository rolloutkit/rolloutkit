"""The sidecar spike reuses traffic code without moving contract evaluation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from preflightkit.engine.events import RequestPhase
from preflightkit.traffic.client import Outcome, RequestResult

ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = ROOT / "spikes" / "sidecar-probe" / "probe.py"


def _probe_module():
    spec = importlib.util.spec_from_file_location("sidecar_probe_spike", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(request_id: int, outcome: Outcome) -> RequestResult:
    return RequestResult(
        request_id=request_id,
        outcome=outcome,
        phase=RequestPhase.AWAITING_RESPONSE,
        status=200 if outcome is Outcome.COMPLETED else None,
        started_ns=1,
        connected_ns=2,
        request_sent_ns=3,
        finished_ns=20,
    )


def test_spike_reports_completion_rate_with_the_population() -> None:
    probe = _probe_module()
    evidence = probe._inflight_evidence(
        [
            _request(1, Outcome.COMPLETED),
            _request(2, Outcome.RESET_BEFORE_RESPONSE),
        ],
        sigterm_ns=10,
    )

    assert evidence["issued"] == 2
    assert evidence["in_flight_at_sigterm"] == 2
    assert evidence["completed"] == 1
    assert evidence["completion_rate"] == 0.5


def test_spike_imports_traffic_but_not_contract_modules() -> None:
    source = PROBE_PATH.read_text()
    assert "preflightkit.traffic.accept_probe" in source
    assert "preflightkit.traffic.generator" in source
    assert "preflightkit.contracts" not in source
