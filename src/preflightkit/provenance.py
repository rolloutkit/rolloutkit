"""Resolve the source revision that produced a measurement."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def preflightkit_commit() -> str:
    """Return an explicit build revision or the enclosing Git checkout HEAD."""
    configured = os.environ.get("PREFLIGHTKIT_COMMIT", "").strip().lower()
    if configured:
        return configured if _COMMIT.fullmatch(configured) else "unknown"

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


def _checkout_root(start: Path) -> Path | None:
    for parent in (start.parent, *start.parents):
        if (parent / ".git").exists():
            return parent
    return None
