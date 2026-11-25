"""Unit tests for refresh locking, 403 error handling, progress sync and translations."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    LoginFailed,
    ProviderPermissionDenied,
    UnplayableMediaError,
)

from fabel import FabelProvider
from fabel.api import FabelApi, ProgressEntry, parse_progress_map


class _FakeResponse:
    """Minimal async context manager emulating aiohttp.ClientResponse."""

    def __init__(
        self, status: int, body: str, url: str = "https://eventyr.fuseapps.no/api"
    ) -> None:
        self.status = status
        self.url = url
        self._body = body

    async def text(self) -> str:
        await asyncio.sleep(0)
        return self._body

    async def json(self, *, content_type: str | None = None) -> Any:
        del content_type
        await asyncio.sleep(0)
        return json.loads(self._body)

    async def release(self) -> None:
        await asyncio.sleep(0)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeSession:
    """Fake aiohttp session tracking concurrent requests and token refreshes."""

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.api_calls: list[tuple[str, str]] = []
        self.force_403_body: str | None = None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        del headers
        if url.endswith("/auth/token"):
            self.refresh_calls += 1
            assert json == {"refreshToken": "rt-initial"}
            return _FakeResponse(
                200,
                '{"access_token": "tok-fresh", "refresh_token": "rt-rotated"}',
                url=url,
            )
        return _FakeResponse(404, '{"detail": "unexpected POST"}', url=url)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        del json, params
        auth = (headers or {}).get("Authorization", "")
        self.api_calls.append((method, auth))
        if self.force_403_body is not None:
            return _FakeResponse(403, self.force_403_body, url=url)
        if auth != "Bearer tok-fresh":
            return _FakeResponse(401, '{"detail": "Token expired"}', url=url)
        return _FakeResponse(200, '{"ok": true}', url=url)


async def test_concurrent_401_triggers_single_refresh() -> None:
    session = _FakeSession()
    rotated: list[tuple[str, str | None]] = []

    api = FabelApi(
        session,  # type: ignore[arg-type]
        access_token="tok-stale",  # noqa: S106
        refresh_token="rt-initial",  # noqa: S106
        on_tokens_updated=lambda: rotated.append((api.access_token, api.refresh_token)),
    )

    results = await asyncio.gather(*(api.request_json("GET", "/me") for _ in range(5)))

    assert results == [{"ok": True}] * 5
    assert session.refresh_calls == 1
    assert api.access_token == "tok-fresh"  # noqa: S105
    assert api.refresh_token == "rt-rotated"  # noqa: S105
    assert len(rotated) == 1


async def test_403_raises_permission_denied_with_fabel_detail() -> None:
    session = _FakeSession()
    session.force_403_body = '{"detail": "Monthly listening allowance reached"}'
    api = FabelApi(
        session,  # type: ignore[arg-type]
        access_token="tok-fresh",  # noqa: S106
        refresh_token="rt-initial",  # noqa: S106
    )

    with pytest.raises(ProviderPermissionDenied) as exc_info:
        await api.request_json("GET", "/book/9788203454332/streamV2")

    assert not isinstance(exc_info.value, LoginFailed)
    assert "Monthly listening allowance reached" in str(exc_info.value)
    assert session.refresh_calls == 0


async def test_401_without_refresh_raises_login_failed() -> None:
    session = _FakeSession()
    api = FabelApi(
        session,  # type: ignore[arg-type]
        access_token="tok-stale",  # noqa: S106
        refresh_token=None,
    )

    with pytest.raises(LoginFailed, match="Token expired"):
        await api.request_json("GET", "/me")


async def test_get_stream_details_surfaces_403_as_unplayable() -> None:
    provider = FabelProvider.__new__(FabelProvider)
    provider.logger = MagicMock()
    session = _FakeSession()
    session.force_403_body = '{"message": "Subscription paused"}'
    provider._api = FabelApi(  # noqa: SLF001
        session,  # type: ignore[arg-type]
        access_token="tok-fresh",  # noqa: S106
    )

    with pytest.raises(UnplayableMediaError, match="Subscription paused"):
        await provider.get_stream_details("9788203454332", MediaType.AUDIOBOOK)


def test_extract_error_detail_variants() -> None:
    assert FabelApi._extract_error_detail('{"detail": "Out of hours"}') == "Out of hours"  # noqa: SLF001
    assert FabelApi._extract_error_detail('{"message": "Not allowed"}') == "Not allowed"  # noqa: SLF001
    assert FabelApi._extract_error_detail("plain text error") == "plain text error"  # noqa: SLF001
    assert FabelApi._extract_error_detail("") == "no error details returned"  # noqa: SLF001


def test_parse_progress_map_handles_list_and_wrapped() -> None:
    payload = [
        {"isbn": "9788203454332", "currentPosition": 3778, "updatedAt": "2026-11-24T12:00:00Z"},
        {"isbn": "9788202863005", "currentPosition": 0},
        {"isbn": "bad", "currentPosition": "not-a-number"},
    ]
    parsed = parse_progress_map(payload)
    assert parsed == {
        "9788203454332": ProgressEntry(3778, datetime(2026, 11, 24, 12, 0, tzinfo=UTC)),
        "9788202863005": ProgressEntry(0, None),
    }
    assert parse_progress_map({"items": [{"isbn": "123", "position": 42.9}]}) == {
        "123": ProgressEntry(42, None)
    }
    assert parse_progress_map(None) == {}


def test_parse_audiobook_sets_resume_position_and_completed() -> None:
    provider = FabelProvider.__new__(FabelProvider)
    provider.config = MagicMock(instance_id="fabel--test")

    # 1. Nested books-in-progress wrapper carries currentPosition + updatedAt
    wrapper = {
        "currentPosition": 1800,
        "status": "in-progress",
        "updatedAt": "2026-11-24T10:30:00Z",
        "book": {"isbn": "9781", "title": "Book One", "duration": 3600},
    }
    book1 = provider._parse_audiobook(wrapper)  # noqa: SLF001
    assert book1 is not None
    assert book1.resume_position_ms == 1_800_000
    assert book1.fully_played is False
    assert book1.resume_position_timestamp == datetime(2026, 11, 24, 10, 30, tzinfo=UTC)

    # 2. Progress map overrides/fills in position, clamps to duration, and carries timestamp
    ts2 = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
    raw2 = {"isbn": "9782", "title": "Book Two", "duration": 1000}
    book2 = provider._parse_audiobook(  # noqa: SLF001
        raw2, progress_map={"9782": ProgressEntry(9999, ts2)}
    )
    assert book2 is not None
    assert book2.resume_position_ms == 1_000_000
    assert book2.fully_played is False
    assert book2.resume_position_timestamp == ts2

    # 3. Completed book marks fully_played=True and resume_position_ms=0
    raw3 = {"isbn": "9783", "title": "Book Three", "duration": 2000}
    book3 = provider._parse_audiobook(  # noqa: SLF001
        raw3, progress_map={"9783": ProgressEntry(1990, ts2)}, fully_played=True
    )
    assert book3 is not None
    assert book3.fully_played is True
    assert book3.resume_position_ms == 0
    assert book3.resume_position_timestamp == ts2


def test_translations_parity_and_no_nl() -> None:
    fabel_dir = Path(__file__).resolve().parents[1] / "fabel"
    en = json.loads((fabel_dir / "strings.json").read_text(encoding="utf-8"))
    nb_path = fabel_dir / "translations" / "nb.json"
    nl_path = fabel_dir / "translations" / "nl.json"

    assert nb_path.is_file()
    assert not nl_path.exists()

    nb = json.loads(nb_path.read_text(encoding="utf-8"))

    def flat_keys(obj: Any, prefix: str = "") -> set[str]:
        if not isinstance(obj, dict):
            return {prefix}
        return {k for name, val in obj.items() for k in flat_keys(val, f"{prefix}{name}.")}

    assert flat_keys(nb) == flat_keys(en)
