"""Fail unless an installed rolloutkit named the revision it was built from.

Run by the wheel-install CI job, against a report produced by an installed
rolloutkit rather than by the repository, with `ROLLOUTKIT_COMMIT` unset.

Every claim this project makes is indexed by `rolloutkit_commit`: a measurement
that cannot say which harness produced it cannot be reproduced, compared, or
argued with, and `docs/field-notes.md` requires the number in every section. The
runtime resolves it from the environment or from an enclosing Git checkout, and
an installed copy has neither — so from 0.1.0 until this check existed, every
report written by every user said `"unknown"`, and nothing in CI could notice,
because CI ran from a checkout with the variable set.

That is why the check belongs here and only here. This is the one job that runs
a wheel from outside the source tree with nothing in the environment to help it,
which makes it the one place where a wrong answer is visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: assert_provenance.py REPORT.json EXPECTED_COMMIT", file=sys.stderr)
        return 2

    report, expected = Path(argv[1]), argv[2].strip().lower()
    commit = (json.loads(report.read_text()).get("rolloutkit_commit") or "").lower()
    if commit == expected:
        print(f"rolloutkit_commit: {commit}")
        return 0

    if commit == "unknown":
        print(
            "rolloutkit_commit is 'unknown'. The build did not stamp the wheel, "
            "so this installation cannot name the revision that produced its "
            "measurements — and neither can any copy of this release that "
            "anybody installs. See hatch_build.py: the stamp is written only "
            "when the build can resolve a revision, and this build could not.",
            file=sys.stderr,
        )
        return 1

    print(
        f"rolloutkit_commit is {commit!r}, expected {expected!r}.\n"
        "The wheel is stamped, but with the wrong revision. A report that names "
        "a plausible wrong commit is worse than one that admits it does not "
        "know: nothing downstream can tell the two apart.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
