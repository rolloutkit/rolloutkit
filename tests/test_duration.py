import pytest

from preflightkit.config.duration import (
    DurationError,
    format_measured_ms,
    format_ms,
    parse_duration_ms,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", 30_000),
        ("500ms", 500),
        ("2m", 120_000),
        ("1h", 3_600_000),
        ("1.5s", 1500),
        ("  10s  ", 10_000),
        ("0s", 0),
    ],
)
def test_parses_units(text: str, expected: int) -> None:
    assert parse_duration_ms(text) == expected


@pytest.mark.parametrize("value", [30, 1.5, "30", "30 seconds", "", None, True, "1.0005s"])
def test_rejects_ambiguous_values(value: object) -> None:
    """A bare number would be read as seconds by one reader and ms by another."""
    with pytest.raises(DurationError):
        parse_duration_ms(value)


@pytest.mark.parametrize(
    ("ms", "expected"), [(500, "500ms"), (1500, "1.5s"), (30_000, "30s"), (90_000, "1m30s")]
)
def test_formats(ms: int, expected: str) -> None:
    assert format_ms(ms) == expected


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        # The measurement this function exists for. service-b's ten readiness
        # probes had a median of 0.596ms and the row read `p50 0ms`.
        (0.596, "0.6ms"),
        (0.004, "0.004ms"),
        (0.841, "0.84ms"),
        (1.0, "1ms"),
        (9.999, "10ms"),
        # At and above the threshold, whole milliseconds — but rounded, where the
        # old `int()` truncated 89.255 to 89 and 289.7 to 289.
        (89.255, "89ms"),
        (289.7, "290ms"),
        (1500.4, "1.5s"),
        (0.0, "0ms"),
    ],
)
def test_formats_a_measurement_without_rounding_it_away(ms: float, expected: str) -> None:
    assert format_measured_ms(ms) == expected


def test_no_positive_measurement_is_ever_rendered_as_zero() -> None:
    """The defect, stated as a property rather than a case.

    A probe that returned is a probe that took time. Whatever the report does to
    that number on the way to the terminal, it may not arrive as `0ms`.
    """
    value = 1e-9
    while value < 10:
        assert format_measured_ms(value) != "0ms", value
        value *= 1.7


def test_configured_durations_keep_their_own_formatter() -> None:
    """`format_ms` is unchanged: `parse_duration_ms` already made it whole."""
    assert format_ms(500) == "500ms"
    assert format_ms(90_000) == "1m30s"
