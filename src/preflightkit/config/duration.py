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
    """Render a *configured* duration, which is whole milliseconds by construction.

    Use `format_measured_ms` for anything that came off a clock.
    """
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.2f}".rstrip("0").rstrip(".") + "s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m{rest:.0f}s"


#: Below this, the fraction of a millisecond *is* the measurement. Above it, the
#: fraction is noise next to what the reader is comparing.
SUB_MS_PRECISION_BELOW_MS = 10.0


def format_measured_ms(ms: float) -> str:
    """Render a measured duration without turning it into a different number.

    Every report used to reach `format_ms` through `int()`, which truncates
    toward zero. For a shutdown of 289.7ms that costs a millisecond nobody
    notices. For readiness it printed a lie: service-b's ten probes measured
    0.596ms at the median and the row read `p50 0ms`, a latency no HTTP probe
    can return, next to a `max 89ms` taken from the same burst. A reader is
    entitled to conclude from that the tool is broken.

    So: keep the fraction where it carries the finding, round rather than
    truncate where it does not, and never render a measured duration as a number
    the probe could not have measured.
    """
    if abs(ms) < SUB_MS_PRECISION_BELOW_MS:
        rendered = f"{ms:.2f}".rstrip("0").rstrip(".")
        # A positive measurement below 5us would round to "0" at two decimals.
        # It has never been observed on any host, and it still may not print as
        # a zero, which is the whole point of this function.
        if rendered in ("0", "-0"):
            return f"{ms:.1g}ms"
        return f"{rendered}ms"
    return format_ms(round(ms))


Duration = Annotated[int, BeforeValidator(parse_duration_ms)]
