"""Unit tests for Fabel allowance duration formatting."""

from __future__ import annotations

from fabel import _format_ms_duration


def test_format_minutes_only() -> None:
    assert _format_ms_duration(65 * 60_000) == "1 t 5 min"
    assert _format_ms_duration(45 * 60_000) == "45 min"
    assert _format_ms_duration(2 * 3_600_000) == "2 t"
    assert _format_ms_duration(0) == "0 min"
