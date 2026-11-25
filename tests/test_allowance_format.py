"""Unit tests for Fabel allowance duration formatting."""

from __future__ import annotations

from fabel import _format_ms_duration, _split_ms_duration


def test_split_ms_duration() -> None:
    assert _split_ms_duration(65 * 60_000) == (1, 5)
    assert _split_ms_duration(45 * 60_000) == (0, 45)
    assert _split_ms_duration(2 * 3_600_000) == (2, 0)
    assert _split_ms_duration(0) == (0, 0)
    assert _split_ms_duration(-10_000) == (0, 0)


def test_format_ms_duration() -> None:
    assert _format_ms_duration(65 * 60_000) == "1h 5m"
    assert _format_ms_duration(45 * 60_000) == "45m"
    assert _format_ms_duration(2 * 3_600_000) == "2h"
    assert _format_ms_duration(0) == "0m"
