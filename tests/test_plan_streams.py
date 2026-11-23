"""Unit tests for Fabel plan → concurrent stream mapping."""

from __future__ import annotations

from fabel.api import concurrent_streams_from_me


def test_standard_plan_is_one_stream() -> None:
    me = {
        "subscription": {
            "planName": "Standard: 100 timer lytting",
            "maxGuests": 0,
        }
    }
    assert concurrent_streams_from_me(me) == 1


def test_duo_plan_is_two_streams() -> None:
    me = {"subscription": {"planName": "Duo", "maxGuests": 1}}
    assert concurrent_streams_from_me(me) == 2


def test_familie_plan_is_three_streams() -> None:
    me = {"subscription": {"planName": "Familie", "maxGuests": 2}}
    assert concurrent_streams_from_me(me) == 3


def test_missing_subscription_defaults_to_one() -> None:
    assert concurrent_streams_from_me({}) == 1
