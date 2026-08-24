"""Duration parsing: "30s", "1500ms", "2m" -> milliseconds."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BeforeValidator

_UNITS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}
_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$", re.IGNORECASE)


class DurationError(ValueError):
    """Raised when a duration string cannot be parsed."""


def parse_duration_ms(value: object) -> int:
    """Parse a duration into whole milliseconds.

    Bare numbers are rejected on purpose: `30` is ambiguous between seconds and
    milliseconds, and a silent misreading here would shift every measurement the
    tool reports.
    """
    if isinstance(value, bool):
        raise DurationError(f"invalid duration: {value!r}")
    if isinstance(value, int):
        raise DurationError(
            f"duration {value!r} has no unit; write '{value}s' or '{value}ms'"
        )
    if isinstance(value, float):
        raise DurationError(
            f"duration {value!r} has no unit; write '{value}s' or '{value}ms'"
        )
    if not isinstance(value, str):
        raise DurationError(f"invalid duration: {value!r}")

    match = _PATTERN.match(value)
    if match is None:
        raise DurationError(
            f"invalid duration {value!r}; expected forms like '30s', '500ms', '2m'"
        )
    amount, unit = match.groups()
    ms = float(amount) * _UNITS_MS[unit.lower()]
    if ms != int(ms):
        raise DurationError(f"duration {value!r} is not a whole number of milliseconds")
    return int(ms)


def format_ms(ms: int) -> str:
    """Render milliseconds the way the reports read best."""
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.2f}".rstrip("0").rstrip(".") + "s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m{rest:.0f}s"


Duration = Annotated[int, BeforeValidator(parse_duration_ms)]
