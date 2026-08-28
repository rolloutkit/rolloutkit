"""Fail unless the run in a JSON report was measured from the sidecar.

Run by the wheel-install CI job, against a report produced by an installed
rolloutkit rather than by the repository.

`probe_location` is the difference between a measurement taken inside the run's
own network and one taken through a published port from the host, and
rolloutkit steps down from the first to the second by itself when a host
cannot support the first. That is the right behaviour, and it is exactly why the
step-down cannot also serve as the alarm: a broken installation produced the
same line as a rootless daemon, and produced it for every user of the release
until this check existed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: assert_sidecar.py REPORT.json", file=sys.stderr)
        return 2

    environment = json.loads(Path(argv[1]).read_text()).get("environment") or {}
    location = environment.get("probe_location")
    if location == "sidecar":
        print(f"probe_location: {location}")
        return 0

    print(
        f"probe_location is {location!r}, not 'sidecar'.\n"
        f"probe_fallback_reason: {environment.get('probe_fallback_reason')}\n"
        "This is an installed copy on a runner that can host a sidecar, so the "
        "fallback is not a property of the machine. Every run this installation "
        "ever makes would be measured through a published port, and the report "
        "would present that as the environment's doing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
