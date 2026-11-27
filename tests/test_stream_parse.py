"""Unit tests for streamV2 response parsing (no network)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType

from fabel import FabelProvider
from fabel.api import FabelApi
from fabel.constants import CDN_TOKEN_TTL

STREAM = {
    "url": "https://fabel-cdn.fuseapps.no/stream/9788203454332.webm",
    "duration": 28835,
    "token": {
        "mac": "lgwfeiNgO%2FFA1HxFENJwbEloNREPW3vXNP%2BA3hDhVjQ%3D",
        "timestamp": 1795344053,
    },
}


def test_parse_stream_keeps_mac_percent_encoded() -> None:
    url, headers, duration = FabelApi.parse_stream_response(STREAM)
    assert url.endswith(".webm")
    # The CDN decodes the mac itself; a decoded "+" would reach it as a space (403).
    assert headers["mac"] == "lgwfeiNgO%2FFA1HxFENJwbEloNREPW3vXNP%2BA3hDhVjQ%3D"
    assert "+" not in headers["mac"]
    assert headers["timestamp"] == "1795344053"
    assert duration == 28835


def test_parse_stream_without_token_sends_no_auth_headers() -> None:
    _, headers, _ = FabelApi.parse_stream_response({"url": STREAM["url"]})
    assert "mac" not in headers
    assert "timestamp" not in headers


async def test_stream_details_expire_before_cdn_refuses_token() -> None:
    provider = FabelProvider.__new__(FabelProvider)
    provider.logger = MagicMock()
    provider.config = MagicMock(instance_id="fabel--test")
    api = MagicMock()
    api.get_book_stream = AsyncMock(return_value=STREAM)
    api.parse_stream_response = FabelApi.parse_stream_response
    provider._api = api  # noqa: SLF001

    details = await provider.get_stream_details("9788203454332", MediaType.AUDIOBOOK)

    # A resume or seek after this many seconds fetches a fresh streamV2 (the CDN refuses a
    # mac/timestamp pair after 60-90 s).
    assert details.expiration == CDN_TOKEN_TTL
    assert CDN_TOKEN_TTL < 60
    assert details.extra_input_args[0] == "-headers"
    assert (
        "mac: lgwfeiNgO%2FFA1HxFENJwbEloNREPW3vXNP%2BA3hDhVjQ%3D\r\n" in details.extra_input_args[1]
    )
