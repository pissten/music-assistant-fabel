"""Tests for Fabel phone normalization / OTP helpers."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import LoginFailed

from fabel.api import FabelApi


def test_normalize_phone_eight_digits() -> None:
    assert FabelApi.normalize_phone("91234567") == "+4791234567"
    assert FabelApi.normalize_phone("91 23 45 67") == "+4791234567"


def test_normalize_phone_e164() -> None:
    assert FabelApi.normalize_phone("+4791234567") == "+4791234567"
    assert FabelApi.normalize_phone("004791234567") == "+4791234567"


def test_normalize_phone_invalid() -> None:
    with pytest.raises(LoginFailed):
        FabelApi.normalize_phone("123")


def test_normalize_otp_preserves_leading_zero_from_int() -> None:
    assert FabelApi.normalize_otp_code(87591) == "087591"
    assert FabelApi.normalize_otp_code("087591") == "087591"
    assert FabelApi.normalize_otp_code(" 08 75 91 ") == "087591"
