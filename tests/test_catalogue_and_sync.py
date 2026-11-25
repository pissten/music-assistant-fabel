"""Unit tests for v0.2.0: Algolia search, Discover/Genres browse, favorites, progress push."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import Audiobook, BrowseFolder, ProviderMapping, Track

from fabel import SUPPORTED_FEATURES, FabelProvider
from fabel.api import FabelApi, ProgressEntry, _normalize_genre_page_size, build_algolia_filters
from fabel.constants import (
    ALGOLIA_APP_ID,
    ALGOLIA_SEARCH_KEY,
    ALGOLIA_SEARCH_URL,
    DOMAIN,
    HEADER_PROFILE_ID,
    HEADER_PROFILE_TYPE,
    HEADER_SUBSCRIPTION_PLAN,
)
from fabel.listening import ListeningTracker


class _FakeResp:
    """Minimal aiohttp response stub."""

    def __init__(self, status: int, body: Any, url: str = "") -> None:
        self.status = status
        self.url = url
        self._text = body if isinstance(body, str) else json.dumps(body)

    async def text(self) -> str:
        await asyncio.sleep(0)
        return self._text

    async def json(self, *, content_type: str | None = None) -> Any:
        del content_type
        await asyncio.sleep(0)
        return json.loads(self._text)

    async def release(self) -> None:
        await asyncio.sleep(0)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RecordingSession:
    """Fake aiohttp session recording requests and dispatching canned responses."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], _FakeResp] = {}
        self.algolia_response: _FakeResp = _FakeResp(200, {"hits": []}, url=ALGOLIA_SEARCH_URL)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
    ) -> _FakeResp:
        self.calls.append(
            {"method": "POST", "url": url, "headers": dict(headers or {}), "json": json}
        )
        if url == ALGOLIA_SEARCH_URL:
            return self.algolia_response
        return _FakeResp(404, {"detail": "unexpected post"}, url=url)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        params: dict[str, str] | None = None,
    ) -> _FakeResp:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "json": json,
                "params": dict(params or {}),
            }
        )
        for (m, suffix), resp in self.responses.items():
            if m == method and url.endswith(suffix):
                return resp
        return _FakeResp(200, {}, url=url)


def _make_provider(api: Any) -> FabelProvider:
    provider = FabelProvider.__new__(FabelProvider)
    provider.config = MagicMock(instance_id="fabel--test")
    provider.logger = MagicMock()
    provider._api = api  # noqa: SLF001
    provider._tracker = ListeningTracker(AsyncMock())  # noqa: SLF001
    provider._last_synced_progress = {}  # noqa: SLF001
    provider._completed_synced = set()  # noqa: SLF001
    return provider


def test_supported_features_include_library_edit() -> None:
    assert ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT in SUPPORTED_FEATURES
    assert ProviderFeature.SEARCH in SUPPORTED_FEATURES
    assert ProviderFeature.BROWSE in SUPPORTED_FEATURES


def test_build_algolia_filters() -> None:
    assert build_algolia_filters(profile_type="adult", only_adult_books=False) is None
    assert (
        build_algolia_filters(profile_type="child", only_adult_books=False)
        == 'primaryGenre:"Barn" OR primaryGenre:"Ungdom"'
    )
    assert (
        build_algolia_filters(profile_type="adult", only_adult_books=True)
        == 'NOT primaryGenre:"Barn" AND NOT primaryGenre:"Ungdom"'
    )
    assert (
        build_algolia_filters(profile_type="child", only_adult_books=True)
        == 'primaryGenre:"Barn" OR primaryGenre:"Ungdom"'
    )


def test_normalize_genre_page_size() -> None:
    assert _normalize_genre_page_size(1) == 10
    assert _normalize_genre_page_size(10) == 10
    assert _normalize_genre_page_size(25) == 30
    assert _normalize_genre_page_size(60) == 100
    assert _normalize_genre_page_size(500) == 100


async def test_api_search_books_headers_and_filters() -> None:
    session = _RecordingSession()
    session.responses[("GET", "/me")] = _FakeResp(
        200,
        {"subscription": {"planName": "Fabel Standard", "features": ["Only adult books"]}},
    )
    session.responses[("GET", "/me/profiles")] = _FakeResp(
        200,
        [{"id": "prof-1", "name": "Default", "type": "adult"}],
    )
    session.algolia_response = _FakeResp(
        200,
        {
            "hits": [
                {
                    "isbn": "9788203454332",
                    "title": "Snømannen",
                    "authors": [{"name": "Jo Nesbø"}],
                    "duration": 36000,
                    "subscriberAvailability": "streamable",
                }
            ]
        },
        url=ALGOLIA_SEARCH_URL,
    )

    api = FabelApi(session, access_token="tok-1")  # type: ignore[arg-type]  # noqa: S106
    await api.get_me()
    await api.ensure_profile("prof-1")

    assert await api.search_books("   ") == []
    hits = await api.search_books("Nesbø", limit=150)

    assert len(hits) == 1
    assert hits[0]["isbn"] == "9788203454332"
    post_call = next(c for c in session.calls if c["url"] == ALGOLIA_SEARCH_URL)
    assert post_call["headers"]["X-Algolia-Application-Id"] == ALGOLIA_APP_ID
    assert post_call["headers"]["X-Algolia-API-Key"] == ALGOLIA_SEARCH_KEY
    assert post_call["json"] == {
        "query": "Nesbø",
        "hitsPerPage": 100,
        "filters": 'NOT primaryGenre:"Barn" AND NOT primaryGenre:"Ungdom"',
    }


async def test_api_search_books_http_error_raises_unavailable() -> None:
    session = _RecordingSession()
    session.algolia_response = _FakeResp(503, {"message": "Algolia down"}, url=ALGOLIA_SEARCH_URL)
    api = FabelApi(session, access_token="tok-1")  # type: ignore[arg-type]  # noqa: S106

    with pytest.raises(ProviderUnavailableError, match="Algolia down"):
        await api.search_books("Harry")


async def test_api_genre_books_encodes_id_snaps_page_size_and_sends_context() -> None:
    session = _RecordingSession()
    session.responses[("GET", "/me")] = _FakeResp(
        200,
        {"subscription": {"planName": "Fabel Familie"}},
    )
    session.responses[("GET", "/me/profiles")] = _FakeResp(
        200,
        [{"id": "prof-1", "name": "Voksen", "type": "adult"}],
    )
    session.responses[("GET", "/genres/Krim%20og%20Spenning/books")] = _FakeResp(
        200,
        [{"isbn": "9781", "title": "Krimbok"}],
    )

    api = FabelApi(session, access_token="tok-1")  # type: ignore[arg-type]  # noqa: S106
    await api.get_me()
    await api.ensure_profile()
    books = await api.get_genre_books("Krim og Spenning", page=2, page_size=25)

    assert len(books) == 1
    call = session.calls[-1]
    assert call["url"].endswith("/genres/Krim%20og%20Spenning/books")
    assert call["params"] == {"page": "2", "pageSize": "30"}
    assert call["headers"][HEADER_PROFILE_ID] == "prof-1"
    assert call["headers"][HEADER_PROFILE_TYPE] == "adult"
    assert call["headers"][HEADER_SUBSCRIPTION_PLAN] == "Fabel Familie"


async def test_api_favorites_and_progress_mutations_and_cache_invalidation() -> None:
    session = _RecordingSession()
    api = FabelApi(
        session,  # type: ignore[arg-type]
        access_token="tok-1",  # noqa: S106
        profile_id="prof-1",
    )
    api._list_cache["favorites"] = (9e12, [{"isbn": "old"}])  # noqa: SLF001
    api._list_cache["progress"] = (9e12, [])  # noqa: SLF001
    api._list_cache["completed"] = (9e12, [])  # noqa: SLF001
    api._list_cache["in_progress"] = (9e12, [])  # noqa: SLF001

    await api.add_favorite("9788203454332")
    assert "favorites" not in api._list_cache  # noqa: SLF001
    assert session.calls[-1]["method"] == "POST"
    assert session.calls[-1]["url"].endswith("/me/profile/favorites")
    assert session.calls[-1]["json"] == {"book": "9788203454332"}

    await api.remove_favorite("9788203454332")
    assert session.calls[-1]["method"] == "DELETE"
    assert session.calls[-1]["url"].endswith("/me/profile/favorites")
    assert session.calls[-1]["json"] == {"book": "9788203454332"}

    # Removing a book that is already not a favorite (404) is idempotent
    session.responses[("DELETE", "/me/profile/favorites")] = _FakeResp(404, {"detail": "Not found"})
    await api.remove_favorite("9788203454332")

    ts = datetime(2026, 11, 25, 8, 30, 0, tzinfo=UTC)
    await api.set_book_progress("9788203454332", 1234, updated_at=ts)
    assert "progress" not in api._list_cache  # noqa: SLF001
    assert "in_progress" not in api._list_cache  # noqa: SLF001
    assert session.calls[-1]["method"] == "PUT"
    assert session.calls[-1]["url"].endswith("/books/9788203454332/progress")
    assert session.calls[-1]["json"] == {
        "position": 1234,
        "updatedAt": "2026-11-25T08:30:00.000Z",
    }

    await api.mark_completed("9788203454332")
    assert "completed" not in api._list_cache  # noqa: SLF001
    assert session.calls[-1]["method"] == "POST"
    assert session.calls[-1]["url"].endswith("/me/profile/completed")
    assert session.calls[-1]["json"] == {"book": "9788203454332"}


async def test_provider_search_uses_algolia_availability_and_fallback() -> None:
    api = MagicMock()
    api.get_progress_map = AsyncMock(return_value={"9781": ProgressEntry(600, None)})
    api.search_books = AsyncMock(
        return_value=[
            {
                "isbn": "9781",
                "title": "Tilgjengelig bok",
                "authors": [{"name": "Forfatter A"}],
                "duration": 3600,
                "primaryGenre": "Krim",
                "subGenres": ["Nordic Noir"],
                "subscriberAvailability": "streamable",
            },
            {
                "isbn": "9782",
                "title": "Kun kjøp",
                "duration": 3600,
                "subscriberAvailability": "purchasable",
            },
        ]
    )
    provider = _make_provider(api)

    res = await provider.search("bok", [MediaType.AUDIOBOOK], limit=10)
    assert [b.item_id for b in res.audiobooks] == ["9781", "9782"]
    first, second = res.audiobooks[0], res.audiobooks[1]
    assert isinstance(first, Audiobook)
    assert isinstance(second, Audiobook)
    assert first.resume_position_ms == 600_000
    assert next(iter(first.provider_mappings)).available is True
    assert first.metadata.genres == {"Krim", "Nordic Noir"}
    assert next(iter(second.provider_mappings)).available is False

    # Algolia failure gracefully falls back to cached library lists
    api.search_books = AsyncMock(side_effect=ProviderUnavailableError("down"))
    api.get_books_in_progress = AsyncMock(return_value=[{"isbn": "9783", "title": "Min bok"}])
    api.get_favorites = AsyncMock(return_value=[])
    fallback = await provider.search("min", [MediaType.AUDIOBOOK], limit=5)
    assert [b.item_id for b in fallback.audiobooks] == ["9783"]


async def test_provider_browse_discover_and_genres() -> None:
    api = MagicMock()
    api.get_progress_map = AsyncMock(return_value={})
    api.get_home_page = AsyncMock(
        return_value={
            "id": "home",
            "items": [
                {"id": "banner-1", "title": "Promo", "resource": "Banner"},
                {"id": "empty-1", "title": "Tom liste", "resource": "BookList", "books": []},
                {
                    "id": "list-nyheter",
                    "title": "Nyheter i Fabel",
                    "resource": "BookList",
                    "books": [
                        {
                            "isbn": "9781",
                            "title": "Ny bok",
                            "duration": 1800,
                            "cover": "https://cdn.example/cover.jpg",
                        }
                    ],
                },
            ],
        }
    )
    api.get_genres = AsyncMock(
        return_value=[
            {
                "id": "Krim og Spenning",
                "shortDisplayName": "Krim",
                "longDisplayName": "Krim og spenning",
            }
        ]
    )
    api.get_genre_books = AsyncMock(
        return_value=[{"isbn": "9789", "title": "Mysteriet", "duration": 2400}]
    )

    provider = _make_provider(api)

    # Discover root lists only non-empty BookLists and uses first book cover as folder image
    discover_items = await provider.browse("fabel--test://discover")
    assert len(discover_items) == 1
    assert isinstance(discover_items[0], BrowseFolder)
    assert discover_items[0].name == "Nyheter i Fabel"
    assert discover_items[0].path == "fabel--test://discover/list-nyheter"
    assert discover_items[0].image is not None
    assert discover_items[0].image.path == "https://cdn.example/cover.jpg"

    # Discover list returns its books
    list_books = await provider.browse("fabel--test://discover/list-nyheter")
    assert len(list_books) == 1
    assert isinstance(list_books[0], Audiobook)
    assert list_books[0].item_id == "9781"

    # Genres root URL-encodes genre ids
    genre_folders = await provider.browse("fabel--test://genres")
    assert len(genre_folders) == 1
    assert isinstance(genre_folders[0], BrowseFolder)
    assert genre_folders[0].name == "Krim og spenning"
    assert genre_folders[0].path == "fabel--test://genres/Krim%20og%20Spenning"

    # Genre leaf decodes genre id and calls get_genre_books
    genre_books = await provider.browse("fabel--test://genres/Krim%20og%20Spenning")
    api.get_genre_books.assert_awaited_once_with(
        "Krim og Spenning", page=1, page_size=100, use_cache=True
    )
    assert len(genre_books) == 1
    assert isinstance(genre_books[0], Audiobook)
    assert genre_books[0].item_id == "9789"


async def test_provider_library_add_and_remove() -> None:
    api = MagicMock()
    api.add_favorite = AsyncMock()
    api.remove_favorite = AsyncMock()
    provider = _make_provider(api)

    # Direct provider item
    prov_book = Audiobook(
        item_id="9781", provider="fabel--test", name="Bok 1", provider_mappings=set()
    )
    assert await provider.library_add(prov_book) is True
    api.add_favorite.assert_awaited_once_with("9781")

    # Library item wrapping a Fabel provider mapping
    api.add_favorite.reset_mock()
    lib_book = Audiobook(
        item_id="42",
        provider="library",
        name="Bok 2",
        provider_mappings={
            ProviderMapping(
                item_id="9782",
                provider_domain=DOMAIN,
                provider_instance="fabel--test",
            )
        },
    )
    assert await provider.library_add(lib_book) is True
    api.add_favorite.assert_awaited_once_with("9782")

    # Non-audiobook items are ignored
    track = Track(item_id="1", provider="fabel--test", name="Song", provider_mappings=set())
    assert await provider.library_add(track) is False
    assert await provider.library_remove("9782", MediaType.TRACK) is False

    # Remove audiobook
    assert await provider.library_remove("9782", MediaType.AUDIOBOOK) is True
    api.remove_favorite.assert_awaited_once_with("9782")


async def test_on_played_pushes_progress_throttled_and_marks_completed_once() -> None:
    api = MagicMock()
    api.set_book_progress = AsyncMock()
    api.mark_completed = AsyncMock()
    provider = _make_provider(api)
    book = Audiobook(
        item_id="9781",
        provider="fabel--test",
        name="Bok",
        duration=1000,
        provider_mappings=set(),
    )

    # First report while playing pushes initial position
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=False,
        position=100,
        media_item=book,
        is_playing=True,
    )
    api.set_book_progress.assert_awaited_once_with("9781", 100)

    # Small delta (<15s) while playing is skipped
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=False,
        position=108,
        media_item=book,
        is_playing=True,
    )
    assert api.set_book_progress.await_count == 1

    # Delta >= 15s while playing pushes
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=False,
        position=130,
        media_item=book,
        is_playing=True,
    )
    assert api.set_book_progress.await_count == 2
    api.set_book_progress.assert_awaited_with("9781", 130)

    # Stop/pause pushes even for a small delta
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=False,
        position=135,
        media_item=book,
        is_playing=False,
    )
    assert api.set_book_progress.await_count == 3
    api.set_book_progress.assert_awaited_with("9781", 135)

    # Repeated pause at identical position does not spam API
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=False,
        position=135,
        media_item=book,
        is_playing=False,
    )
    assert api.set_book_progress.await_count == 3

    # Completion marks completed once
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=True,
        position=1000,
        media_item=book,
        is_playing=False,
    )
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=True,
        position=1000,
        media_item=book,
        is_playing=False,
    )
    api.mark_completed.assert_awaited_once_with("9781")

    # API errors during progress push never propagate
    api.set_book_progress = AsyncMock(side_effect=RuntimeError("boom"))
    await provider.on_played(
        MediaType.AUDIOBOOK,
        "9781",
        fully_played=False,
        position=200,
        media_item=book,
        is_playing=False,
    )
