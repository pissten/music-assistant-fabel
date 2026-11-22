"""
Fabel audiobook provider for Music Assistant.

Unofficial community provider for Fabel subscriptions. Talks to Fuse Eventyr
(``eventyr.fuseapps.no``) with the user's bearer token and ``x-profile-id``.
Streams via ``/api/book/{isbn}/streamV2`` → progressive ``.webm`` (Opus) on
``fabel-cdn.fuseapps.no`` with ``mac`` + ``timestamp`` headers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
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

from music_assistant.models.music_provider import MusicProvider

from .api import FabelApi
from .constants import (
    CONF_ACCESS_TOKEN,
    CONF_PROFILE_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize one Fabel provider instance."""
    return FabelProvider(mass, manifest, config, SUPPORTED_FEATURES)


class FabelProvider(MusicProvider):
    """Music source for Fabel audiobooks."""

    _api: FabelApi

    @property
    def is_streaming_provider(self) -> bool:
        """Fabel is a streaming catalogue / subscription library."""
        return True

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """No runtime options yet — credentials live in setup_data."""
        return ()

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
        )
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
        self.logger.info(
            "Fabel login OK for %s (profile=%s)",
            _safe_user_label(me),
            self._api.profile_id,
        )

    def _persist_tokens(self) -> None:
        """Write rotated access/refresh tokens back into provider setup data."""
        if not self._api:
            return
        try:
            self._update_config_value(
                CONF_ACCESS_TOKEN, self._api.access_token, immediate=True
            )
            if self._api.refresh_token:
                self._update_config_value(
                    CONF_REFRESH_TOKEN, self._api.refresh_token, immediate=True
                )
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Could not persist Fabel tokens: %s", err)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search in-progress / favorites locally until catalogue search is mapped."""
        result = SearchResults()
        if MediaType.AUDIOBOOK not in media_types:
            return result
        query = search_query.lower().strip()
        books: list[Audiobook] = []
        for loader in (self._api.get_books_in_progress, self._api.get_favorites):
            try:
                payload = await loader()
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Fabel search source failed: %s", err)
                continue
            for raw in _iter_book_dicts(payload):
                book = self._parse_audiobook(raw)
                if book and (not query or query in book.name.lower()):
                    books.append(book)
                if len(books) >= limit:
                    break
            if len(books) >= limit:
                break
        result.audiobooks = books[:limit]
        return result

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield in-progress, favorite and completed books."""
        seen: set[str] = set()
        for loader in (
            self._api.get_books_in_progress,
            self._api.get_favorites,
            self._api.get_completed,
        ):
            try:
                payload = await loader()
            except Exception as err:  # noqa: BLE001
                self.logger.warning("Fabel library fetch failed: %s", err)
                continue
            for raw in _iter_book_dicts(payload):
                book = self._parse_audiobook(raw)
                if not book or book.item_id in seen:
                    continue
                seen.add(book.item_id)
                yield book

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Return one audiobook by ISBN via /api/book/{isbn}."""
        try:
            raw = await self._api.get_book(prov_audiobook_id)
            book = self._parse_audiobook(raw if isinstance(raw, dict) else {})
            if book:
                return book
        except MediaNotFoundError:
            raise
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Fabel get_book failed, scanning library: %s", err)
        async for book in self.get_library_audiobooks():
            if book.item_id == prov_audiobook_id:
                return book
        raise MediaNotFoundError(f"Fabel book not found: {prov_audiobook_id}")

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """
        Browse Fabel via the standard library folders.

        MA passes ``{instance_id}://…`` (not ``fabel://…``). Use the base MusicProvider
        browse so «Lydbøker» and Bla gjennom stay in sync with the library.
        """
        # Custom folders (påbegynt/favoritter/fullført) as extra roots when at provider root.
        path_parts = path.split("://", 1)
        subpath = ""
        if len(path_parts) == 2:
            subpath = path_parts[1].strip("/")

        if not subpath:
            folders: list[BrowseFolder] = []
            # Standard library audiobooks folder from the base implementation
            folders.extend(await super().browse(path))
            root = path if path.endswith("://") or path.endswith("/") else f"{path.rstrip('/')}/"
            if "://" not in root:
                root = f"{self.instance_id}://"
            for key, name in (
                ("in_progress", "Påbegynt"),
                ("favorites", "Favoritter"),
                ("completed", "Fullført"),
            ):
                folders.append(
                    BrowseFolder(
                        item_id=key,
                        provider=self.instance_id,
                        path=f"{root}{key}",
                        name=name,
                    )
                )
            return folders

        if subpath in ("in_progress", "favorites", "completed"):
            loader = {
                "in_progress": self._api.get_books_in_progress,
                "favorites": self._api.get_favorites,
                "completed": self._api.get_completed,
            }[subpath]
            payload = await loader()
            books: list[Audiobook] = []
            for raw in _iter_book_dicts(payload):
                book = self._parse_audiobook(raw)
                if book:
                    books.append(book)
            return books

        return await super().browse(path)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve stream details for a Fabel audiobook."""
        if media_type != MediaType.AUDIOBOOK:
            raise UnplayableMediaError("Fabel provider only streams audiobooks")

        try:
            stream = await self._api.get_book_stream(item_id)
        except MediaNotFoundError:
            raise
        except Exception as err:
            raise UnplayableMediaError(f"Fabel streamV2 failed for {item_id}: {err}") from err

        cdn_url, cdn_headers, duration = self._api.parse_stream_response(stream)
        # Progressive WebM/Opus (Media3 ProgressiveMediaPeriod + HTTP 206).
        content_type = getattr(ContentType, "OGG", None) or getattr(
            ContentType, "UNKNOWN", ContentType.UNKNOWN
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
        )

    def _parse_audiobook(self, raw: dict[str, Any]) -> Audiobook | None:
        """Best-effort map of Eventyr book DTO → MA Audiobook."""
        # Prefer ISBN: streamV2 and CDN are keyed by isbn.
        book_id = str(
            raw.get("isbn")
            or raw.get("id")
            or raw.get("bookId")
            or raw.get("uuid")
            or ""
        )
        title = str(raw.get("title") or raw.get("name") or "").strip()
        if not book_id or not title:
            nested = raw.get("book") if isinstance(raw.get("book"), dict) else None
            if nested:
                return self._parse_audiobook(nested)
            return None

        authors = _as_name_list(raw.get("authors") or raw.get("author"))
        narrators = _as_name_list(raw.get("narrators") or raw.get("narrator"))
        duration = _as_int(raw.get("duration") or raw.get("durationSeconds") or raw.get("length")) or 0
        image_url = (
            raw.get("cover")
            or raw.get("coverUrl")
            or raw.get("image")
            or raw.get("imageUrl")
            or ""
        )
        if isinstance(image_url, dict):
            image_url = image_url.get("url") or image_url.get("href") or ""

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
                    available=True,
                    audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                )
            },
        )
        description = str(raw.get("description") or raw.get("summary") or "").strip()
        if description:
            book.metadata.description = description
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
                "name": str(ch.get("title") or ch.get("name") or f"Kapittel {idx + 1}"),
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

        return book


def _safe_user_label(me: dict[str, Any]) -> str:
    return str(
        me.get("given_name")
        or me.get("name")
        or me.get("sub")
        or me.get("id")
        or "user"
    )


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
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
