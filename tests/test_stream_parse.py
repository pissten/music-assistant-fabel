"""Unit tests for streamV2 response parsing (no network)."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote


def _parse(stream: dict[str, Any]) -> tuple[str, dict[str, str], int]:
    """Mirror of FabelApi.parse_stream_response without importing MA deps."""
    base = str(stream.get("url") or "").strip()
    raw_token = stream.get("token")
    token: dict[str, Any] = raw_token if isinstance(raw_token, dict) else {}
    mac = str(token.get("mac") or "").strip()
    timestamp = token.get("timestamp")
    headers = {"User-Agent": "okhttp/4.12.0", "Accept": "*/*"}
    if mac and timestamp is not None:
        headers["mac"] = unquote(mac)
        headers["timestamp"] = str(timestamp)
    return base, headers, int(stream.get("duration") or 0)


def test_parse_stream_unquotes_mac() -> None:
    url, headers, duration = _parse(
        {
            "url": "https://fabel-cdn.fuseapps.no/stream/9788203454332.webm",
            "duration": 28835,
            "token": {
                "mac": "lgwfeiNgO%2FFA1HxFENJwbEloNREPW3vXNP%2BA3hDhVjQ%3D",
                "timestamp": 1795344053,
            },
        }
    )
    assert url.endswith(".webm")
    assert "%2F" not in headers["mac"]
    assert headers["mac"].endswith("=")
    assert headers["timestamp"] == "1795344053"
    assert duration == 28835
