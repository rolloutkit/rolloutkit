"""Resolve the source revision that produced a measurement."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def rolloutkit_commit() -> str:
    """Return the revision this copy runs, or `"unknown"` if it cannot be known.

    Three sources, in order. An explicit `ROLLOUTKIT_COMMIT` wins, because a
    caller that names a revision is describing a copy the other two cannot see —
    CI runs the matrix that way. Then the revision a build froze into the
    package, which is the only thing an installed copy carries. Then the
    checkout this source file belongs to, for anyone running from a clone.

    An answer that cannot be trusted is not softened into a nearby one: each
    source either produces a revision or the string `"unknown"`, and a report
    that says `"unknown"` is readable evidence that the copy could not identify
    itself. A wrong revision would not be.
    """
    configured = os.environ.get("ROLLOUTKIT_COMMIT", "").strip().lower()
    if configured:
        return configured if _COMMIT.fullmatch(configured) else "unknown"

    stamped = _stamped_commit()
    if stamped is not None:
        return stamped

    checkout = _checkout_root(Path(__file__).resolve())
    if checkout is None:
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip().lower()
    return commit if result.returncode == 0 and _COMMIT.fullmatch(commit) else "unknown"


def _stamped_commit() -> str | None:
    """The revision the build froze in, or `None` when this copy was not built.

    `hatch_build.py` writes the module this reads. A checkout does not have it,
    which is what `None` means here — not "no revision", but "ask Git next".

    A stamp that is present and malformed returns `"unknown"` rather than
    falling through. The fallthrough would search for a checkout above
    `site-packages`, and for an installed copy any checkout found there belongs
    to somebody else.
    """
    try:
        from rolloutkit._build_info import COMMIT
    except ImportError:
        return None

    stamped = str(COMMIT).strip().lower()
    return stamped if _COMMIT.fullmatch(stamped) else "unknown"


def _checkout_root(start: Path) -> Path | None:
    """The checkout this file is part of — not merely one it happens to sit in.

    A venv created inside an unrelated project puts `site-packages` under that
    project's `.git`, and walking up from an installed copy then reports that
    project's HEAD as the revision rolloutkit was built from. That is worse than
    reporting nothing, because it looks like an answer: the field is what the
    measurement corpus is indexed by, and one plausible wrong SHA is
    indistinguishable from a right one until somebody tries to check out.

    So the nearest enclosing checkout counts only if it is the one that tracks
    this file. Anything else is somebody else's repository, and the search stops
    rather than climbing into a still less related one.
    """
    package = start.parent
    for parent in (package, *package.parents):
        if (parent / ".git").exists():
            return parent if (parent / "src" / package.name) == package else None
    return None
