#!/usr/bin/env python3
"""Build and execute the sidecar probe spike matrix."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROBE_IMAGE = "pfk-sidecar-probe:spike"

FIXTURES: dict[str, dict[str, Any]] = {
    "delayed-bind": {
        "image": "pfk-fixture-stdlib",
        "context": "fixtures/stdlib-http",
        "env": {"HANDLE_SIGTERM": "1", "STARTUP_DELAY_SECONDS": "3"},
    },
    "immediate-in-app": {
        "image": "pfk-fixture-drain-window",
        "context": "fixtures/drain-window",
        "env": {"DRAIN_SECONDS": "0", "EXIT_SECONDS": "0.3"},
    },
    "drains-in-app": {
        "image": "pfk-fixture-drain-window",
        "context": "fixtures/drain-window",
        "env": {"DRAIN_SECONDS": "1.8", "EXIT_SECONDS": "2.0"},
    },
    "kills-inflight": {
        "image": "pfk-fixture-kills",
        "context": "fixtures/kills-inflight",
        "env": {},
        "inflight_path": "/slow",
        "concurrent": 10,
        "sigterm_after_ms": 2000,
    },
}


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() if capture else "see command output above"
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed


def _build() -> None:
    _run(["docker", "pull", "busybox:latest"])
    _run(
        [
            "docker",
            "build",
            "-t",
            PROBE_IMAGE,
            "-f",
            "spikes/sidecar-probe/Dockerfile",
            ".",
        ]
    )
    built: set[str] = set()
    for fixture in FIXTURES.values():
        image = fixture["image"]
        if image in built:
            continue
        _run(["docker", "build", "-t", image, fixture["context"]])
        built.add(image)


def _probe_command(
    *,
    mode: str,
    environment: str,
    fixture_name: str,
    fixture: dict[str, Any],
    network: str,
    run_name: str,
) -> list[str]:
    launch_ns = time.time_ns()
    arguments = [
        "--environment",
        environment,
        "--probe-location",
        mode,
        "--fixture",
        fixture_name,
        "--network",
        network,
        "--name",
        run_name,
        "--target-image",
        fixture["image"],
        "--target-env-json",
        json.dumps(fixture["env"], separators=(",", ":")),
        "--launch-requested-unix-ns",
        str(launch_ns),
    ]
    if fixture.get("inflight_path"):
        arguments += [
            "--inflight-path",
            fixture["inflight_path"],
            "--concurrent",
            str(fixture["concurrent"]),
            "--sigterm-after-ms",
            str(fixture["sigterm_after_ms"]),
        ]
    if mode == "host":
        return [sys.executable, "spikes/sidecar-probe/probe.py", *arguments]
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        PROBE_IMAGE,
        *arguments,
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("host", "sidecar"), required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    if not args.no_build:
        _build()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for fixture_name, fixture in FIXTURES.items():
        for repeat in range(1, args.repeats + 1):
            token = uuid.uuid4().hex[:10]
            network = f"pfk-sidecar-{token}"
            run_name = f"pfk-sidecar-{token}"
            _run(["docker", "network", "create", network], capture=True)
            try:
                command = _probe_command(
                    mode=args.mode,
                    environment=args.environment,
                    fixture_name=fixture_name,
                    fixture=fixture,
                    network=network,
                    run_name=run_name,
                )
                completed = _run(command, capture=True)
                result = json.loads(completed.stdout)
                result["repeat"] = repeat
                destination = args.output_dir / f"{fixture_name}-{repeat}.json"
                destination.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, sort_keys=True))
            finally:
                subprocess.run(
                    ["docker", "network", "rm", network],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )


if __name__ == "__main__":
    main()
