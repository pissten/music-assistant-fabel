"""
Fabel audiobook provider for Music Assistant.

Unofficial community provider for Fabel subscriptions. Talks to Fuse Eventyr
(``eventyr.fuseapps.no``) with the user's bearer token and ``x-profile-id``.
Streams via ``/api/book/{isbn}/streamV2`` → progressive ``.webm`` (Opus) on
``fabel-cdn.fuseapps.no`` with ``mac`` + ``timestamp`` headers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, unquote

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    SetupFailedError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    BrowseFolder,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .api import FabelApi, ProgressEntry, concurrent_streams_from_me, parse_iso_utc
from .constants import (
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    CDN_TOKEN_TTL,
    CONF_ACCESS_TOKEN,
    CONF_PROFILE_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    PROGRESS_SYNC_MIN_DELTA_SECONDS,
)
from .listening import ListeningTracker

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize one Fabel provider instance."""
    return FabelProvider(mass, manifest, config, SUPPORTED_FEATURES)


class FabelProvider(MusicProvider):
    """Music source for Fabel audiobooks."""

    _api: FabelApi
    _max_concurrent_streams: int = 1
    _tracker: ListeningTracker | None = None
    _last_synced_progress: dict[str, int]
    _completed_synced: set[str]

    @property
    def is_streaming_provider(self) -> bool:
        """Fabel is a streaming catalogue / subscription library."""
        return True

    @property
    def max_concurrent_streams(self) -> int:
        """Honour Fabel plan limits (Lett/Standard=1, Duo=2, Familie=3)."""
        return self._max_concurrent_streams

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Show Fabel listening allowance (spent / remaining hours) on the settings page."""
        return (await self._allowance_config_entry(),)

    async def _allowance_config_entry(self) -> ConfigEntry:
        """Build a localized allowance label entry from Eventyr `/me/allowance` (+ plan)."""
        try:
            me = await self._api.get_me()
            allowance_payload = await self._api.get_allowance()
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Fabel allowance fetch failed: %s", err)
            return ConfigEntry(
                key="fabel_allowance_unavailable",
                type=ConfigEntryType.LABEL,
                label="Listening time: currently unavailable (reopen settings to retry).",
                category="generic",
            )

        raw_sub = me.get("subscription")
        sub: dict[str, Any] = raw_sub if isinstance(raw_sub, dict) else {}
        plan_name = str(sub.get("planName") or "").strip() or "unknown plan"
        streams = concurrent_streams_from_me(me)

        block: dict[str, Any] = allowance_payload if isinstance(allowance_payload, dict) else {}
        nested = block.get("allowance")
        allowance: dict[str, Any]
        if isinstance(nested, dict):
            allowance = nested
        else:
            raw_allowance = sub.get("allowance")
            allowance = raw_allowance if isinstance(raw_allowance, dict) else {}

        spent_ms = _as_int(allowance.get("spentTime")) or 0
        available_ms = _as_int(allowance.get("availableTime"))
        if available_ms is None:
            actual = _as_int(allowance.get("actualAllowance")) or 0
            available_ms = max(0, actual - spent_ms)

        spent_h, spent_m = _split_ms_duration(spent_ms)
        left_h, left_m = _split_ms_duration(available_ms)
        return ConfigEntry(
            key="fabel_allowance",
            type=ConfigEntryType.LABEL,
            label=(
                f"Listening time this period: {spent_h}h {spent_m}m used · "
                f"{left_h}h {left_m}m remaining ({plan_name}, max {streams} concurrent streams)."
            ),
            translation_params=[
                str(spent_h),
                str(spent_m),
                str(left_h),
                str(left_m),
                plan_name,
                str(streams),
            ],
            category="generic",
        )

    async def handle_async_init(self) -> None:
        """Validate tokens and resolve listening profile."""
        access = str(self.get_setup_value(CONF_ACCESS_TOKEN) or "")
        refresh = str(self.get_setup_value(CONF_REFRESH_TOKEN) or "")
        profile = str(self.get_setup_value(CONF_PROFILE_ID) or "")
        if not access and not refresh:
            raise SetupFailedError("Fabel access token or refresh token is required")
        self._api = FabelApi(
            self.mass.http_session,
            access_token=access or "pending",
            refresh_token=refresh or None,
            profile_id=profile or None,
            on_tokens_updated=self._persist_tokens,
            throttler=Throttler(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD),
        )
        self._tracker = ListeningTracker(self._api.report_listening_sessions)
        self._last_synced_progress = {}
        self._completed_synced = set()
        if not access and refresh:
            await self._api.refresh_access_token()
            self._persist_tokens()
        try:
            me = await self._api.get_me()
            await self._api.ensure_profile(profile or None)
        except LoginFailed as err:
            if refresh:
                try:
                    await self._api.refresh_access_token()
                    self._persist_tokens()
                    me = await self._api.get_me()
                    await self._api.ensure_profile(profile or None)
                except LoginFailed as refresh_err:
                    raise SetupFailedError(str(refresh_err)) from refresh_err
            else:
                raise SetupFailedError(str(err)) from err
        self._max_concurrent_streams = concurrent_streams_from_me(me)
        self.logger.info(
            "Fabel login OK for %s (profile=%s streams=%s)",
            _safe_user_label(me),
            self._api.profile_id,
            self._max_concurrent_streams,
        )

    def _persist_tokens(self) -> None:
        """Write rotated access/refresh tokens back into provider setup data."""
        if not self._api:
            return
        try:
            self._update_config_value(CONF_ACCESS_TOKEN, self._api.access_token, immediate=True)
            if self._api.refresh_token:
                self._update_config_value(
                    CONF_REFRESH_TOKEN, self._api.refresh_token, immediate=True
                )
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Could not persist Fabel tokens: %s", err)

    async def _safe_progress_map(self) -> dict[str, ProgressEntry]:
        """Return cached ISBN → ProgressEntry map from Fabel without failing the caller."""
        try:
            return await self._api.get_progress_map(use_cache=True)
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Fabel progress sync fetch failed: %s", err)
            return {}

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search the full Fabel catalogue via Algolia, with cached-library fallback."""
        result = SearchResults()
        if MediaType.AUDIOBOOK not in media_types:
            return result
        clean_query = search_query.strip()
        if not clean_query:
            return result

        progress_map = await self._safe_progress_map()
        try:
            hits = await self._api.search_books(clean_query, limit=limit)
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Fabel catalogue search failed, falling back to library: %s", err)
            result.audiobooks = await self._search_library_fallback(
                clean_query, limit=limit, progress_map=progress_map
            )
            return result

        books: list[Audiobook] = []
        seen: set[str] = set()
        for raw in hits:
            book = self._parse_audiobook(raw, progress_map=progress_map)
            if not book or book.item_id in seen:
                continue
            seen.add(book.item_id)
            books.append(book)
            if len(books) >= limit:
                break
        result.audiobooks = books
        return result

    async def _search_library_fallback(
        self,
        query: str,
        *,
        limit: int,
        progress_map: dict[str, ProgressEntry],
    ) -> list[Audiobook]:
        """Fallback search over cached in-progress and favorite books."""
        needle = query.lower()
        books: list[Audiobook] = []
        seen: set[str] = set()
        for loader in (self._api.get_books_in_progress, self._api.get_favorites):
            try:
                payload = await loader(use_cache=True)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Fabel fallback search source failed: %s", err)
                continue
            for raw in _iter_book_dicts(payload):
                book = self._parse_audiobook(raw, progress_map=progress_map)
                if not book or book.item_id in seen:
                    continue
                if needle in book.name.lower():
                    seen.add(book.item_id)
                    books.append(book)
                if len(books) >= limit:
                    return books
        return books

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an audiobook to the active Fabel profile's favorites."""
        if item.media_type != MediaType.AUDIOBOOK:
            return False
        isbn = self._extract_fabel_isbn(item)
        if not isbn:
            return False
        await self._api.add_favorite(isbn)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an audiobook from the active Fabel profile's favorites."""
        if media_type != MediaType.AUDIOBOOK or not prov_item_id:
            return False
        await self._api.remove_favorite(prov_item_id)
        return True

    def _extract_fabel_isbn(self, item: MediaItemType) -> str | None:
        """Resolve the Fabel ISBN from either a provider item or a library item."""
        if item.provider in (self.instance_id, DOMAIN) and item.item_id:
            return str(item.item_id).strip() or None
        for mapping in getattr(item, "provider_mappings", ()) or ():
            if (
                mapping.provider_instance == self.instance_id or mapping.provider_domain == DOMAIN
            ) and mapping.item_id:
                return str(mapping.item_id).strip() or None
        return str(item.item_id).strip() or None

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield in-progress, favorite and completed books with Fabel progress."""
        progress_map = await self._safe_progress_map()
        seen: set[str] = set()
        for loader, is_completed in (
            (self._api.get_books_in_progress, False),
            (self._api.get_favorites, None),
            (self._api.get_completed, True),
        ):
            try:
                payload = await loader(use_cache=True)
            except LoginFailed:
                raise
            except ProviderPermissionDenied as err:
                self.logger.warning("Fabel denied library access: %s", err)
                continue
            except Exception as err:  # noqa: BLE001
                self.logger.warning("Fabel library fetch failed: %s", err)
                continue
            for raw in _iter_book_dicts(payload):
                book = self._parse_audiobook(
                    raw, progress_map=progress_map, fully_played=is_completed
                )
                if not book or book.item_id in seen:
                    continue
                seen.add(book.item_id)
                yield book

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Return one audiobook by ISBN via /api/book/{isbn}."""
        try:
            raw = await self._api.get_book(prov_audiobook_id)
        except MediaNotFoundError, LoginFailed, ProviderPermissionDenied:
            raise
        except Exception as err:
            raise MediaNotFoundError(f"Fabel book not found: {prov_audiobook_id}") from err
        progress_map = await self._safe_progress_map()
        book = self._parse_audiobook(
            raw if isinstance(raw, dict) else {}, progress_map=progress_map
        )
        if book:
            return book
        raise MediaNotFoundError(f"Fabel book not found: {prov_audiobook_id}")

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """
        Browse Fabel via the standard library folders.

        MA passes ``{instance_id}://…`` (not ``fabel://…``). Use the base MusicProvider
        browse so Audiobooks and Browse stay in sync with the library.
        """
        # Custom folders (in_progress/favorites/completed) as extra roots when at provider root.
        path_parts = path.split("://", 1)
        subpath = ""
        if len(path_parts) == 2:
            subpath = path_parts[1].strip("/")

        root = f"{self.instance_id}://"
        if "://" in path:
            root = f"{path_parts[0]}://"

        if not subpath:
            # Standard library audiobooks folder from the base implementation
            folders: list[BrowseFolder] = [
                item for item in await super().browse(path) if isinstance(item, BrowseFolder)
            ]
            for key, name in (
                ("in_progress", "In progress"),
                ("favorites", "Favorites"),
                ("completed", "Completed"),
                ("discover", "Discover"),
                ("genres", "Genres"),
            ):
                folders.append(
                    BrowseFolder(
                        item_id=key,
                        provider=self.instance_id,
                        path=f"{root}{key}",
                        name=name,
                        translation_key=key,
                    )
                )
            return folders

        custom_items = await self._browse_custom_subpath(root, subpath)
        if custom_items is not None:
            return custom_items

        return cast(
            "Sequence[MediaItemType | BrowseFolder]",
            await super().browse(path),
        )

    async def _browse_custom_subpath(
        self, root: str, subpath: str
    ) -> Sequence[MediaItemType | BrowseFolder] | None:
        """Resolve Fabel-specific browse paths, returning None for unknown subpaths."""
        if subpath in ("in_progress", "favorites", "completed"):
            loader = {
                "in_progress": self._api.get_books_in_progress,
                "favorites": self._api.get_favorites,
                "completed": self._api.get_completed,
            }[subpath]
            payload = await loader(use_cache=True)
            progress_map = await self._safe_progress_map()
            is_completed: bool | None = True if subpath == "completed" else None
            return self._parse_book_list(
                _iter_book_dicts(payload),
                progress_map=progress_map,
                fully_played=is_completed,
            )
        if subpath == "discover":
            return await self._browse_discover_sections(root)
        if subpath.startswith("discover/"):
            return await self._browse_discover_list(unquote(subpath.split("/", 1)[1]))
        if subpath == "genres":
            return await self._browse_genres(root)
        if subpath.startswith("genres/"):
            return await self._browse_genre_books(unquote(subpath.split("/", 1)[1]))
        return None

    def _parse_book_list(
        self,
        raw_books: Sequence[dict[str, Any]],
        *,
        progress_map: dict[str, ProgressEntry],
        fully_played: bool | None = None,
    ) -> list[Audiobook]:
        """Parse and deduplicate a sequence of Eventyr/Algolia book payloads."""
        books: list[Audiobook] = []
        seen: set[str] = set()
        for raw in raw_books:
            book = self._parse_audiobook(raw, progress_map=progress_map, fully_played=fully_played)
            if not book or book.item_id in seen:
                continue
            seen.add(book.item_id)
            books.append(book)
        return books

    async def _browse_discover_sections(self, root: str) -> list[BrowseFolder]:
        """Return curated book-list folders from Fabel's home page."""
        home = await self._api.get_home_page(use_cache=True)
        folders: list[BrowseFolder] = []
        for list_id, title, raw_books in _iter_book_lists(home):
            folder_image = _first_book_image(raw_books, provider=self.instance_id)
            folders.append(
                BrowseFolder(
                    item_id=list_id,
                    provider=self.instance_id,
                    path=f"{root}discover/{quote(list_id, safe='')}",
                    name=title,
                    image=folder_image,
                )
            )
        return folders

    async def _browse_discover_list(self, list_id: str) -> list[Audiobook]:
        """Return books from one curated home-page section."""
        home = await self._api.get_home_page(use_cache=True)
        progress_map = await self._safe_progress_map()
        for section_id, _title, raw_books in _iter_book_lists(home):
            if section_id == list_id:
                return self._parse_book_list(raw_books, progress_map=progress_map)
        return []

    async def _browse_genres(self, root: str) -> list[BrowseFolder]:
        """Return top-level Fabel genre folders."""
        genres = await self._api.get_genres(use_cache=True)
        folders: list[BrowseFolder] = []
        for entry in genres:
            genre_id = str(entry.get("id") or "").strip()
            if not genre_id:
                continue
            label = str(
                entry.get("longDisplayName") or entry.get("shortDisplayName") or genre_id
            ).strip()
            folders.append(
                BrowseFolder(
                    item_id=genre_id,
                    provider=self.instance_id,
                    path=f"{root}genres/{quote(genre_id, safe='')}",
                    name=label,
                )
            )
        return folders

    async def _browse_genre_books(self, genre_id: str) -> list[Audiobook]:
        """Return books in a Fabel genre."""
        raw_books = await self._api.get_genre_books(genre_id, page=1, page_size=100, use_cache=True)
        progress_map = await self._safe_progress_map()
        return self._parse_book_list(raw_books, progress_map=progress_map)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve stream details for a Fabel audiobook."""
        if media_type != MediaType.AUDIOBOOK:
            raise UnplayableMediaError("Fabel provider only streams audiobooks")

        try:
            stream = await self._api.get_book_stream(item_id)
        except MediaNotFoundError, LoginFailed:
            raise
        except ProviderPermissionDenied as err:
            raise UnplayableMediaError(str(err)) from err
        except Exception as err:
            raise UnplayableMediaError(f"Fabel streamV2 failed for {item_id}: {err}") from err

        cdn_url, cdn_headers, duration = self._api.parse_stream_response(stream)
        # Progressive WebM/Opus (Media3 ProgressiveMediaPeriod + HTTP 206).
        content_type = cast(
            "ContentType",
            getattr(ContentType, "OGG", ContentType.UNKNOWN),
        )
        # ffmpeg needs CDN auth as request headers (not query string).
        header_lines = "".join(f"{k}: {v}\r\n" for k, v in cdn_headers.items())
        extra_input_args = ["-headers", header_lines] if header_lines else []

        self.logger.debug(
            "Fabel stream for %s: duration=%s auth_headers=%s",
            item_id,
            duration,
            sorted(k for k in cdn_headers if k.lower() in ("mac", "timestamp")),
        )

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=cdn_url,
            extra_input_args=extra_input_args,
            can_seek=True,
            allow_seek=True,
            duration=duration or None,
            # The CDN refuses a mac/timestamp pair after 60-90 s, so a resume or seek that
            # opens the source again must fetch a fresh streamV2 instead of reusing these.
            expiration=CDN_TOKEN_TTL,
        )

    async def unload(self, is_removed: bool = False) -> None:  # noqa: FBT001, FBT002
        """Flush any queued listening sessions before the provider unloads."""
        if self._tracker is not None:
            await self._tracker.flush()
        await super().unload(is_removed)

    async def on_stream_started(self, streamdetails: StreamDetails) -> None:
        """Anchor the listening interval at the exact seek position when audio starts."""
        if self._tracker is None or streamdetails.media_type != MediaType.AUDIOBOOK:
            return
        await self._tracker.on_stream_started(
            isbn=streamdetails.item_id,
            start_position=int(streamdetails.seek_position or 0),
            book_duration=int(streamdetails.duration or 0),
        )

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Retry uploading any pending listening sessions when a stream completes."""
        if self._tracker is None or streamdetails.media_type != MediaType.AUDIOBOOK:
            return
        await self._tracker.flush()

    async def on_played(  # noqa: PLR0913, PLR0917
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,  # noqa: FBT001
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,  # noqa: FBT001, FBT002
    ) -> None:
        """
        Report listening progress so Fabel counts subscription hours.

        Music Assistant calls this about every 30s while playing, and on pause/stop.
        Intervals are tracked in ``ListeningTracker`` and flushed via
        ``POST /listening-sessions/batch`` with automatic retry on failure.
        """
        if self._tracker is None or media_type != MediaType.AUDIOBOOK:
            return
        if not isinstance(media_item, Audiobook):
            return
        book_duration = int(media_item.duration or 0)
        if book_duration <= 0:
            return
        safe_pos = max(0, int(position or 0))
        await self._tracker.on_played(
            isbn=prov_item_id,
            position=safe_pos,
            book_duration=book_duration,
            fully_played=fully_played,
            is_playing=is_playing,
        )
        await self._sync_progress_to_fabel(
            isbn=prov_item_id,
            position=safe_pos,
            book_duration=book_duration,
            fully_played=fully_played,
            is_playing=is_playing,
        )

    async def _sync_progress_to_fabel(
        self,
        *,
        isbn: str,
        position: int,
        book_duration: int,
        fully_played: bool,
        is_playing: bool,
    ) -> None:
        """Best-effort push of playback position / completion back to the Fabel app."""
        if not hasattr(self, "_last_synced_progress"):
            self._last_synced_progress = {}
        if not hasattr(self, "_completed_synced"):
            self._completed_synced = set()

        clamped_pos = min(max(0, position), book_duration)
        try:
            if fully_played:
                if isbn not in self._completed_synced:
                    if clamped_pos > 0:
                        await self._api.set_book_progress(isbn, clamped_pos)
                        self._last_synced_progress[isbn] = clamped_pos
                    await self._api.mark_completed(isbn)
                    self._completed_synced.add(isbn)
                return

            self._completed_synced.discard(isbn)
            if clamped_pos <= 0:
                return
            last_pos = self._last_synced_progress.get(isbn)
            should_push = (
                (not is_playing and last_pos != clamped_pos)
                or last_pos is None
                or abs(clamped_pos - last_pos) >= PROGRESS_SYNC_MIN_DELTA_SECONDS
            )
            if should_push:
                await self._api.set_book_progress(isbn, clamped_pos)
                self._last_synced_progress[isbn] = clamped_pos
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Fabel progress push failed for %s: %s", isbn, err)

    def _parse_audiobook(  # noqa: C901
        self,
        raw: dict[str, Any],
        *,
        progress_map: dict[str, ProgressEntry] | None = None,
        fully_played: bool | None = None,
        _wrapper_position: int | None = None,
        _wrapper_status: str | None = None,
        _wrapper_updated_at: datetime | None = None,
    ) -> Audiobook | None:
        """Best-effort map of Eventyr book DTO → MA Audiobook (with resume position)."""
        wrapper_pos = (
            _wrapper_position
            if _wrapper_position is not None
            else _as_int(raw.get("currentPosition"))
        )
        wrapper_status = _wrapper_status or str(raw.get("status") or "").strip().lower()
        wrapper_updated_at = _wrapper_updated_at or parse_iso_utc(
            raw.get("updatedAt") or raw.get("lastPlayedAt")
        )

        # Prefer ISBN: streamV2 and CDN are keyed by isbn.
        book_id = str(
            raw.get("isbn") or raw.get("id") or raw.get("bookId") or raw.get("uuid") or ""
        )
        title = str(raw.get("title") or raw.get("name") or "").strip()
        if not book_id or not title:
            nested = raw.get("book") if isinstance(raw.get("book"), dict) else None
            if nested:
                return self._parse_audiobook(
                    nested,
                    progress_map=progress_map,
                    fully_played=fully_played,
                    _wrapper_position=wrapper_pos,
                    _wrapper_status=wrapper_status,
                    _wrapper_updated_at=wrapper_updated_at,
                )
            return None

        authors = _as_name_list(raw.get("authors") or raw.get("author"))
        narrators = _as_name_list(raw.get("narrators") or raw.get("narrator"))
        duration = (
            _as_int(raw.get("duration") or raw.get("durationSeconds") or raw.get("length")) or 0
        )
        image_url = (
            raw.get("cover") or raw.get("coverUrl") or raw.get("image") or raw.get("imageUrl") or ""
        )
        if isinstance(image_url, dict):
            image_url = image_url.get("url") or image_url.get("href") or ""

        available = _is_book_available(raw)
        book = Audiobook(
            item_id=book_id,
            provider=self.instance_id,
            name=title,
            duration=duration,
            authors=UniqueList(authors),
            narrators=UniqueList(narrators),
            provider_mappings={
                ProviderMapping(
                    item_id=book_id,
                    provider_domain=DOMAIN,
                    provider_instance=self.instance_id,
                    available=available,
                    audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                )
            },
        )
        description = str(raw.get("description") or raw.get("summary") or "").strip()
        if description:
            book.metadata.description = description
        genres = _extract_genres(raw)
        if genres:
            book.metadata.genres = genres
        if image_url:
            book.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=str(image_url),
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

        chapters: list[MediaItemChapter] = []
        for idx, ch in enumerate(raw.get("chapters") or []):
            if not isinstance(ch, dict):
                continue
            end = _as_float(ch.get("end"))
            kwargs: dict[str, Any] = {
                "position": idx + 1,
                "name": str(ch.get("title") or ch.get("name") or f"Chapter {idx + 1}"),
                "start": _as_float(ch.get("start") or ch.get("position") or 0) or 0.0,
            }
            if end is not None:
                kwargs["end"] = end
            try:
                chapters.append(MediaItemChapter(**kwargs))
            except TypeError:
                # Older/newer models may differ slightly; skip bad chapter rows.
                continue
        if chapters:
            book.metadata.chapters = chapters

        _apply_resume_state(
            book,
            entry=progress_map.get(book_id) if progress_map else None,
            wrapper_pos=wrapper_pos,
            wrapper_status=wrapper_status,
            wrapper_updated_at=wrapper_updated_at,
            fully_played=fully_played,
        )
        return book


def _apply_resume_state(  # noqa: PLR0913
    book: Audiobook,
    *,
    entry: ProgressEntry | None,
    wrapper_pos: int | None,
    wrapper_status: str,
    wrapper_updated_at: datetime | None,
    fully_played: bool | None,
) -> None:
    """Apply Fabel resume position, completion state and timestamp onto ``book``."""
    updated_at = (entry.updated_at if entry else None) or wrapper_updated_at
    if fully_played is True or wrapper_status == "completed":
        book.fully_played = True
        book.resume_position_ms = 0
        if updated_at is not None:
            book.resume_position_timestamp = updated_at
        return

    pos_sec = entry.position_seconds if entry is not None else wrapper_pos
    if pos_sec is not None and pos_sec > 0:
        pos_ms = pos_sec * 1000
        if book.duration > 0:
            pos_ms = min(pos_ms, book.duration * 1000)
        book.resume_position_ms = pos_ms
        book.fully_played = False
        if updated_at is not None:
            book.resume_position_timestamp = updated_at


def _safe_user_label(me: dict[str, Any]) -> str:
    return str(me.get("given_name") or me.get("name") or me.get("sub") or me.get("id") or "user")


def _iter_book_dicts(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "books", "data", "results", "content"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return [payload]
    return []


def _iter_book_lists(payload: Any) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Extract ``(id, title, books)`` tuples from Eventyr home-page / desked-page items."""
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []
    result: list[tuple[str, str, list[dict[str, Any]]]] = []
    seen_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("resource") != "BookList":
            continue
        raw_books = item.get("books")
        if not isinstance(raw_books, list):
            continue
        books = [b for b in raw_books if isinstance(b, dict)]
        if not books:
            continue
        list_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not list_id or not title or list_id in seen_ids:
            continue
        seen_ids.add(list_id)
        result.append((list_id, title, books))
    return result


def _is_book_available(raw: dict[str, Any]) -> bool:
    """Return False when Eventyr/Algolia explicitly flags a book as non-streamable."""
    for key in ("subscriberAvailability", "streamingStatus"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower() == "streamable"
    return True


def _first_book_image(
    raw_books: Sequence[dict[str, Any]], *, provider: str
) -> MediaItemImage | None:
    """Return a thumbnail image from the first book in a list that has cover art."""
    for raw in raw_books:
        nested = raw.get("book")
        candidate: dict[str, Any] = nested if isinstance(nested, dict) else raw
        image_url = (
            candidate.get("cover")
            or candidate.get("coverUrl")
            or candidate.get("image")
            or candidate.get("imageUrl")
            or ""
        )
        if isinstance(image_url, dict):
            image_url = image_url.get("url") or image_url.get("href") or ""
        if isinstance(image_url, str) and image_url.strip():
            return MediaItemImage(
                type=ImageType.THUMB,
                path=image_url.strip(),
                provider=provider,
                remotely_accessible=True,
            )
    return None


def _extract_genres(raw: dict[str, Any]) -> set[str]:
    """Extract genre labels from Algolia or Eventyr book DTOs."""
    genres: set[str] = set()
    primary = raw.get("primaryGenre")
    if isinstance(primary, str) and primary.strip():
        genres.add(primary.strip())
    for key in ("subGenres", "genres"):
        val = raw.get(key)
        if isinstance(val, list):
            for entry in val:
                if isinstance(entry, str) and entry.strip():
                    genres.add(entry.strip())
                elif isinstance(entry, dict):
                    name = entry.get("name") or entry.get("longDisplayName") or entry.get("id")
                    if isinstance(name, str) and name.strip():
                        genres.add(name.strip())
    return genres


def _as_name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("fullName") or item.get("title")
                if name:
                    names.append(str(name))
        return names
    if isinstance(value, dict):
        name = value.get("name") or value.get("fullName")
        return [str(name)] if name else []
    return []


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except TypeError, ValueError:
        return None


def _split_ms_duration(ms: int) -> tuple[int, int]:
    """Split an Eventyr millisecond duration into whole ``(hours, minutes)``."""
    total_minutes = max(0, int(ms) // 60_000)
    return divmod(total_minutes, 60)


def _format_ms_duration(ms: int) -> str:
    """Format Eventyr millisecond durations as 'Xh Ym' (or just hours/minutes)."""
    hours, minutes = _split_ms_duration(ms)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except TypeError, ValueError:
        return None
