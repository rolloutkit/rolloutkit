import pytest

from preflightkit.config.duration import DurationError, format_ms, parse_duration_ms


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
