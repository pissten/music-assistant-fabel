"""Async client for the Fabel / Eventyr API."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import string
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from aiohttp import ClientError, ClientResponse, ClientSession
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
)

from music_assistant.helpers.throttle_retry import Throttler

from .constants import (
    API_BASE,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    CDN_USER_AGENT,
    EP_ALLOWANCE,
    EP_AUTH_COMPLETE_SIGNIN,
    EP_AUTH_INITIATE_SIGNIN,
    EP_AUTH_TOKEN,
    EP_BOOK,
    EP_BOOK_CAN_LISTEN,
    EP_BOOK_STREAM_V2,
    EP_BOOKS_IN_PROGRESS,
    EP_BOOKSHELVES,
    EP_COMPLETED,
    EP_FAVORITES,
    EP_LISTENING_SESSIONS_BATCH,
    EP_ME,
    EP_PROFILES,
    EP_PROGRESS,
    HEADER_PROFILE_ID,
    LIBRARY_CACHE_TTL_SECONDS,
    SMS_APP_HASH,
    USER_AGENT,
)
from .listening import ListeningSession

LOGGER = logging.getLogger("music_assistant.fabel.api")


class FabelApi:
    """Thin Eventyr API wrapper used by the provider."""

    def __init__(  # noqa: PLR0913
        self,
        session: ClientSession,
        *,
        access_token: str,
        refresh_token: str | None = None,
        profile_id: str | None = None,
        on_tokens_updated: Callable[[], Awaitable[None] | None] | None = None,
        throttler: Throttler | None = None,
    ) -> None:
        """Create an API client with optional token-refresh callback."""
        self._session = session
        self._access_token = access_token.strip()
        self._refresh_token = (refresh_token or "").strip() or None
        self._profile_id = (profile_id or "").strip() or None
        self._on_tokens_updated = on_tokens_updated
        self._throttler = throttler or Throttler(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)
        self._refresh_lock = asyncio.Lock()
        self._list_cache: dict[str, tuple[float, Any]] = {}

    @property
    def access_token(self) -> str:
        """Current bearer access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Current refresh token (Eventyr app-api)."""
        return self._refresh_token

    @property
    def profile_id(self) -> str | None:
        """Active listening profile id (`x-profile-id`)."""
        return self._profile_id

    def invalidate_library_cache(self) -> None:
        """Drop cached in-progress / favorites / completed payloads."""
        self._list_cache.clear()

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Normalize a Norwegian mobile number to E.164 (+47…)."""
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith("0047"):
            digits = digits[2:]
        if len(digits) == 10 and digits.startswith("47"):
            return f"+{digits}"
        if len(digits) == 8:
            return f"+47{digits}"
        if phone.strip().startswith("+") and len(digits) >= 8:
            return f"+{digits}"
        raise LoginFailed("Invalid mobile number. Enter 8 digits or +47...")

    @staticmethod
    def _otp_nonce(length: int = 11) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _headers(self, *, require_profile: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._profile_id:
            headers[HEADER_PROFILE_ID] = self._profile_id
        elif require_profile:
            raise LoginFailed("Fabel profile id is required (x-profile-id)")
        return headers

    @staticmethod
    def _extract_error_detail(body: str) -> str:
        """Extract Fabel's own error message from a JSON or plain-text error body."""
        text = body.strip()
        if not text:
            return "no error details returned"
        try:
            parsed = json.loads(text)
        except ValueError:
            return text[:200]
        if isinstance(parsed, dict):
            for key in ("detail", "message", "error_description", "error", "title"):
                val = parsed.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:200]
        return text[:200]

    async def _raise_for_status(self, resp: ClientResponse) -> None:
        if resp.status < 400:
            return
        detail = self._extract_error_detail(await resp.text())
        if resp.status == 401:
            raise LoginFailed(f"Fabel authentication failed (401): {detail}")
        if resp.status == 403:
            raise ProviderPermissionDenied(f"Fabel denied access (403): {detail}")
        if resp.status == 404:
            raise MediaNotFoundError(f"Fabel resource not found: {resp.url}")
        raise ProviderUnavailableError(f"Fabel HTTP {resp.status}: {detail}")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_data: Any | None = None,
        params: dict[str, str] | None = None,
        require_profile: bool = False,
    ) -> Any:
        """Perform an authenticated JSON request against the Eventyr API."""
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        try:
            used_token = self._access_token
            async with (
                self._throttler,
                self._session.request(
                    method,
                    url,
                    headers=self._headers(require_profile=require_profile),
                    json=json_data,
                    params=params,
                ) as resp,
            ):
                if resp.status == 401 and self._refresh_token:
                    await resp.release()
                    await self.refresh_access_token(failed_token=used_token)
                    async with (
                        self._throttler,
                        self._session.request(
                            method,
                            url,
                            headers=self._headers(require_profile=require_profile),
                            json=json_data,
                            params=params,
                        ) as retry,
                    ):
                        await self._raise_for_status(retry)
                        if retry.status == 204:
                            return {}
                        return await retry.json(content_type=None)
                await self._raise_for_status(resp)
                if resp.status == 204:
                    return {}
                return await resp.json(content_type=None)
        except (
            LoginFailed,
            MediaNotFoundError,
            ProviderPermissionDenied,
            ProviderUnavailableError,
        ):
            raise
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"Fabel request failed: {err}") from err

    async def refresh_access_token(self, *, failed_token: str | None = None) -> str:
        """
        Refresh via Eventyr `POST /api/auth/token` (app refreshToken).

        Serialized behind a lock so concurrent 401 responses only trigger a single
        refresh request (important if Fabel rotates refresh tokens).
        """
        async with self._refresh_lock:
            if failed_token and self._access_token != failed_token:
                # Another concurrent request already refreshed the access token.
                return self._access_token
            if not self._refresh_token:
                raise LoginFailed("No Fabel refresh token configured")
            async with (
                self._throttler,
                self._session.post(
                    f"{API_BASE}{EP_AUTH_TOKEN}",
                    json={"refreshToken": self._refresh_token},
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ) as resp,
            ):
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise LoginFailed(f"Token refresh failed ({resp.status}): {body}")
                token = body.get("access_token")
                if not token:
                    raise LoginFailed("Token refresh returned no access_token")
                self._access_token = token
                new_refresh = body.get("refresh_token") or body.get("refreshToken")
                if new_refresh:
                    self._refresh_token = str(new_refresh)
                LOGGER.info("Fabel access token refreshed")
                if self._on_tokens_updated:
                    result = self._on_tokens_updated()
                    if result is not None:
                        await result
                return self._access_token

    @staticmethod
    def normalize_otp_code(code: Any) -> str:
        """Keep leading zeros — MA/UI may coerce numeric OTP strings to int."""
        if code is None:
            return ""
        if isinstance(code, int):
            # 087591 → 87591 if parsed as int; Fabel OTP is 6 digits
            digits = str(code)
            if len(digits) < 6:
                digits = digits.zfill(6)
            return digits
        return "".join(c for c in str(code) if c.isdigit())

    async def request_sms_otp(self, phone_number: str, *, accept_terms: bool) -> str:
        """Send SMS OTP via Eventyr `POST /api/auth/initiate-signin`. Returns state."""
        if not accept_terms:
            raise LoginFailed("Accept Fabel's terms and privacy policy before signing in")
        phone = self.normalize_phone(phone_number)
        state = self._otp_nonce()
        # Existing-account login: terms + privacy only (no signupTerms — avoids creating accounts).
        payload = {
            "phoneNumber": phone,
            "appHash": SMS_APP_HASH,
            "nonce": self._otp_nonce(),
            "legalConsents": {
                "termsAndConditions": 1,
                "privacyPolicy": 1,
            },
            "state": state,
        }
        try:
            async with (
                self._throttler,
                self._session.post(
                    f"{API_BASE}{EP_AUTH_INITIATE_SIGNIN}",
                    json=payload,
                    headers={
                        "User-Agent": CDN_USER_AGENT,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ) as resp,
            ):
                if resp.status >= 400:
                    detail = self._extract_error_detail(await resp.text())
                    raise LoginFailed(f"Could not send SMS code ({resp.status}): {detail}")
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"Fabel OTP request failed: {err}") from err
        LOGGER.info("Fabel SMS OTP requested for %s… (state_len=%s)", phone[:6], len(state))
        return state

    async def complete_sms_otp(self, phone_number: str, code: str, state: str) -> dict[str, str]:
        """Verify SMS via Eventyr `POST /api/auth/complete-signin` → JWT tokens."""
        phone = self.normalize_phone(phone_number)
        otp = self.normalize_otp_code(code)
        if len(otp) < 4:
            raise LoginFailed("Enter the SMS code you received")
        if not state:
            raise LoginFailed("OTP session missing — restart login with your mobile number")
        payload = {
            "phoneNumber": phone,
            "code": otp,
            "nonce": self._otp_nonce(),
            "state": state,
            "appHash": SMS_APP_HASH,
        }
        LOGGER.info(
            "Fabel complete-signin phone=%s… otp_len=%s state_len=%s",
            phone[:6],
            len(otp),
            len(state),
        )
        try:
            async with (
                self._throttler,
                self._session.post(
                    f"{API_BASE}{EP_AUTH_COMPLETE_SIGNIN}",
                    json=payload,
                    headers={
                        "User-Agent": CDN_USER_AGENT,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ) as resp,
            ):
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    detail = body.get("detail") if isinstance(body, dict) else body
                    raise LoginFailed(f"SMS login failed ({resp.status}): {detail}")
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"Fabel OTP verify failed: {err}") from err

        if not isinstance(body, dict):
            raise LoginFailed("Unexpected response from Fabel complete-signin")
        access = body.get("access_token") or body.get("accessToken")
        refresh = body.get("refresh_token") or body.get("refreshToken")
        if not access:
            raise LoginFailed(f"No access token returned after SMS login (keys={sorted(body)})")
        self._access_token = str(access)
        if refresh:
            self._refresh_token = str(refresh)
        LOGGER.info(
            "Fabel SMS OTP OK (access_len=%s refresh=%s)",
            len(self._access_token),
            bool(self._refresh_token),
        )
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token or "",
        }

    async def get_me(self) -> dict[str, Any]:
        """Return the authenticated user profile root."""
        payload = await self.request_json("GET", EP_ME)
        return payload if isinstance(payload, dict) else {}

    async def get_profiles(self) -> list[dict[str, Any]]:
        """Return listening profiles for the account."""
        payload = await self.request_json("GET", EP_PROFILES)
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            for key in ("profiles", "data", "items"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [p for p in val if isinstance(p, dict)]
        return []

    async def ensure_profile(self, preferred_id: str | None = None) -> str:
        """Resolve and store a profile id for `x-profile-id`."""
        if preferred_id:
            self._profile_id = preferred_id.strip()
            return self._profile_id
        if self._profile_id:
            return self._profile_id
        profiles = await self.get_profiles()
        if not profiles:
            raise LoginFailed("No Fabel listening profiles on this account")
        # Prefer adult / Default, else first
        chosen = next(
            (
                p
                for p in profiles
                if str(p.get("type") or "").lower() == "adult"
                or str(p.get("name") or "").lower() == "default"
            ),
            profiles[0],
        )
        pid = str(chosen.get("id") or "").strip()
        if not pid:
            raise LoginFailed("Fabel profile is missing id")
        self._profile_id = pid
        LOGGER.info("Fabel using profile %s (%s)", pid, chosen.get("name"))
        return pid

    async def _cached_list(self, key: str, path: str) -> Any:
        now = time.monotonic()
        cached = self._list_cache.get(key)
        if cached and (now - cached[0]) < LIBRARY_CACHE_TTL_SECONDS:
            return cached[1]
        payload = await self.request_json("GET", path, require_profile=True)
        self._list_cache[key] = (now, payload)
        return payload

    async def get_books_in_progress(self, *, use_cache: bool = True) -> Any:
        """Return books currently in progress for the profile."""
        if use_cache:
            return await self._cached_list("in_progress", EP_BOOKS_IN_PROGRESS)
        return await self.request_json("GET", EP_BOOKS_IN_PROGRESS, require_profile=True)

    async def get_favorites(self, *, use_cache: bool = True) -> Any:
        """Return favorite books."""
        if use_cache:
            return await self._cached_list("favorites", EP_FAVORITES)
        return await self.request_json("GET", EP_FAVORITES, require_profile=True)

    async def get_completed(self, *, use_cache: bool = True) -> Any:
        """Return completed books."""
        if use_cache:
            return await self._cached_list("completed", EP_COMPLETED)
        return await self.request_json("GET", EP_COMPLETED, require_profile=True)

    async def get_progress(self, *, use_cache: bool = True) -> Any:
        """Return listening progress payload (cached alongside library lists)."""
        if use_cache:
            return await self._cached_list("progress", EP_PROGRESS)
        return await self.request_json("GET", EP_PROGRESS, require_profile=True)

    async def get_progress_map(self, *, use_cache: bool = True) -> dict[str, ProgressEntry]:
        """Return ``{isbn: ProgressEntry}`` from Fabel's progress endpoint."""
        try:
            payload = await self.get_progress(use_cache=use_cache)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Fabel get_progress failed: %s", err)
            return {}
        return parse_progress_map(payload)

    async def get_allowance(self) -> Any:
        """Return listening allowance / hours info."""
        return await self.request_json("GET", EP_ALLOWANCE)

    async def get_bookshelves(self) -> Any:
        """Return bookshelves."""
        return await self.request_json("GET", EP_BOOKSHELVES)

    async def get_book(self, isbn: str) -> dict[str, Any]:
        """Return book metadata by ISBN."""
        payload = await self.request_json("GET", EP_BOOK.format(isbn=isbn), require_profile=True)
        return payload if isinstance(payload, dict) else {}

    async def can_listen(self, isbn: str) -> Any:
        """Return whether the current profile may stream this ISBN."""
        return await self.request_json(
            "GET", EP_BOOK_CAN_LISTEN.format(isbn=isbn), require_profile=True
        )

    async def get_book_stream(self, isbn: str) -> dict[str, Any]:
        """
        Resolve progressive stream for an ISBN.

        ``GET /api/book/{isbn}/streamV2?platform=android`` with ``x-profile-id``.
        Response: ``{url, duration, token:{mac,timestamp}}``.
        """
        path = EP_BOOK_STREAM_V2.format(isbn=isbn)
        payload = await self.request_json(
            "GET",
            path,
            params={"platform": "android"},
            require_profile=True,
        )
        return payload if isinstance(payload, dict) else {}

    async def report_listening_sessions(self, sessions: Sequence[ListeningSession]) -> None:
        """
        Report one or more listening intervals in a single batch call.

        Uses ``POST /listening-sessions/batch`` (verified to increase ``allowance.spentTime``
        for every interval in the batch, including overlapping re-listens).
        Positions and ``bookDuration`` are in whole seconds.
        """
        if not self._profile_id:
            raise LoginFailed("Fabel profile id is required to report listening")
        valid_sessions = [
            item
            for item in sessions
            if item.end_position > item.start_position and item.book_duration > 0
        ]
        if not valid_sessions:
            return
        payload = [
            {
                "isbn": item.isbn,
                "profile": self._profile_id,
                "startPosition": int(item.start_position),
                "endPosition": int(item.end_position),
                "startedAt": _iso_z(item.started_at),
                "endedAt": _iso_z(item.ended_at),
                "bookDuration": int(item.book_duration),
            }
            for item in valid_sessions
        ]
        await self.request_json(
            "POST",
            EP_LISTENING_SESSIONS_BATCH,
            json_data=payload,
            require_profile=True,
        )
        # Progress / library lists may change after reporting
        self.invalidate_library_cache()
        total_seconds = sum(item.seconds for item in valid_sessions)
        LOGGER.debug(
            "Fabel listening batch reported: %s session(s), %ss total",
            len(valid_sessions),
            total_seconds,
        )

    async def report_listening_session(  # noqa: PLR0913
        self,
        *,
        isbn: str,
        start_position: int,
        end_position: int,
        started_at: datetime,
        ended_at: datetime,
        book_duration: int,
    ) -> None:
        """Report a single listening interval via ``report_listening_sessions``."""
        await self.report_listening_sessions(
            [
                ListeningSession(
                    isbn=isbn,
                    start_position=start_position,
                    end_position=end_position,
                    started_at=started_at,
                    ended_at=ended_at,
                    book_duration=book_duration,
                )
            ]
        )

    @staticmethod
    def parse_stream_response(stream: dict[str, Any]) -> tuple[str, dict[str, str], int]:
        """Parse streamV2 JSON into CDN URL, request headers, and duration."""
        base = str(stream.get("url") or "").strip()
        raw_token = stream.get("token")
        token: dict[str, Any] = raw_token if isinstance(raw_token, dict) else {}
        mac = str(token.get("mac") or "").strip()
        timestamp = token.get("timestamp")
        if not base:
            raise MediaNotFoundError("Fabel streamV2 returned no url")
        headers: dict[str, str] = {"User-Agent": CDN_USER_AGENT, "Accept": "*/*"}
        if mac and timestamp is not None:
            headers["mac"] = unquote(mac)
            headers["timestamp"] = str(timestamp)
        duration = int(stream.get("duration") or 0)
        return base, headers, duration

    async def probe_cdn(self, cdn_url: str, headers: dict[str, str]) -> dict[str, str]:
        """Range-probe the CDN URL with stream auth headers."""
        req_headers = {**headers, "Range": "bytes=0-1"}
        try:
            async with self._session.get(
                cdn_url, headers=req_headers, allow_redirects=True
            ) as resp:
                return {k.lower(): v for k, v in resp.headers.items()}
        except ClientError as err:
            LOGGER.debug("CDN probe failed: %s", err)
            return {}


def _iso_z(value: datetime) -> str:
    """Format datetime as UTC ISO-8601 with millisecond precision and Z suffix."""
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def concurrent_streams_from_me(me: dict[str, Any]) -> int:
    """
    Map Fabel subscription plan to max concurrent streams.

    Lett/Standard = 1, Duo = 2, Familie = 3. Falls back to maxGuests+1 or 1.
    """
    raw_sub = me.get("subscription")
    sub: dict[str, Any] = raw_sub if isinstance(raw_sub, dict) else {}
    plan_name = str(sub.get("planName") or "").lower()
    if "familie" in plan_name or "family" in plan_name:
        return 3
    if "duo" in plan_name:
        return 2
    try:
        max_guests = int(sub.get("maxGuests") or 0)
    except TypeError, ValueError:
        max_guests = 0
    if max_guests > 0:
        return max(1, max_guests + 1)
    return 1


@dataclass(frozen=True, slots=True)
class ProgressEntry:
    """One book's listening position reported by Eventyr `/me/profile/progress`."""

    position_seconds: int
    updated_at: datetime | None = None


def parse_iso_utc(value: Any) -> datetime | None:
    """Parse an Eventyr ISO-8601 timestamp into an aware UTC ``datetime``."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_progress_map(payload: Any) -> dict[str, ProgressEntry]:
    """Parse `/me/profile/progress` into ``{isbn: ProgressEntry}``."""
    entries: list[Any]
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = []
        for key in ("items", "progress", "data", "books"):
            val = payload.get(key)
            if isinstance(val, list):
                entries = val
                break
    else:
        return {}

    result: dict[str, ProgressEntry] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        isbn = str(item.get("isbn") or item.get("bookId") or "").strip()
        raw_pos = item.get("currentPosition")
        if raw_pos is None:
            raw_pos = item.get("position")
        if not isbn or raw_pos is None:
            continue
        try:
            pos = max(0, int(float(raw_pos)))
        except TypeError, ValueError:
            continue
        updated_at = parse_iso_utc(item.get("updatedAt") or item.get("lastPlayedAt"))
        result[isbn] = ProgressEntry(position_seconds=pos, updated_at=updated_at)
    return result
