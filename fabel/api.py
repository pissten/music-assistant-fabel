"""Async client for the Fabel / Eventyr API."""

from __future__ import annotations

import logging
import secrets
import string
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote

from aiohttp import ClientError, ClientResponse, ClientSession
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, ProviderUnavailableError

from .constants import (
    API_BASE,
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
    EP_ME,
    EP_PROFILES,
    EP_PROGRESS,
    HEADER_PROFILE_ID,
    SMS_APP_HASH,
    USER_AGENT,
)

LOGGER = logging.getLogger("music_assistant.fabel.api")


class FabelApi:
    """Thin Eventyr API wrapper used by the provider."""

    def __init__(
        self,
        session: ClientSession,
        *,
        access_token: str,
        refresh_token: str | None = None,
        profile_id: str | None = None,
        on_tokens_updated: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        """Create an API client with optional token-refresh callback."""
        self._session = session
        self._access_token = access_token.strip()
        self._refresh_token = (refresh_token or "").strip() or None
        self._profile_id = (profile_id or "").strip() or None
        self._on_tokens_updated = on_tokens_updated

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
        raise LoginFailed("Ugyldig mobilnummer. Bruk 8 siffer eller +47…")

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

    async def _raise_for_status(self, resp: ClientResponse) -> None:
        if resp.status < 400:
            return
        body = await resp.text()
        if resp.status in (401, 403):
            raise LoginFailed(f"Fabel auth failed ({resp.status}): {body[:200]}")
        if resp.status == 404:
            raise MediaNotFoundError(f"Fabel resource not found: {resp.url}")
        raise ProviderUnavailableError(f"Fabel HTTP {resp.status}: {body[:200]}")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        require_profile: bool = False,
    ) -> Any:
        """Perform an authenticated JSON request against the Eventyr API."""
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(require_profile=require_profile),
                json=json_data,
                params=params,
            ) as resp:
                if resp.status == 401 and self._refresh_token:
                    await resp.release()
                    await self.refresh_access_token()
                    async with self._session.request(
                        method,
                        url,
                        headers=self._headers(require_profile=require_profile),
                        json=json_data,
                        params=params,
                    ) as retry:
                        await self._raise_for_status(retry)
                        if retry.status == 204:
                            return {}
                        return await retry.json(content_type=None)
                await self._raise_for_status(resp)
                if resp.status == 204:
                    return {}
                return await resp.json(content_type=None)
        except LoginFailed, MediaNotFoundError, ProviderUnavailableError:
            raise
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"Fabel request failed: {err}") from err

    async def refresh_access_token(self) -> str:
        """Refresh via Eventyr `POST /api/auth/token` (app refreshToken)."""
        if not self._refresh_token:
            raise LoginFailed("No Fabel refresh token configured")
        async with self._session.post(
            f"{API_BASE}{EP_AUTH_TOKEN}",
            json={"refreshToken": self._refresh_token},
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        ) as resp:
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

    async def request_sms_otp(self, phone_number: str) -> str:
        """Send SMS OTP via Eventyr `POST /api/auth/initiate-signin`. Returns state."""
        phone = self.normalize_phone(phone_number)
        state = self._otp_nonce()
        payload = {
            "phoneNumber": phone,
            "appHash": SMS_APP_HASH,
            "nonce": self._otp_nonce(),
            "legalConsents": {
                "termsAndConditions": 1,
                "privacyPolicy": 1,
                "signupTerms": 1,
            },
            "state": state,
        }
        try:
            async with self._session.post(
                f"{API_BASE}{EP_AUTH_INITIATE_SIGNIN}",
                json=payload,
                headers={
                    "User-Agent": CDN_USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise LoginFailed(f"Kunne ikke sende SMS ({resp.status}): {body[:200]}")
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"Fabel OTP request failed: {err}") from err
        LOGGER.info("Fabel SMS OTP requested for %s… (state_len=%s)", phone[:6], len(state))
        return state

    async def complete_sms_otp(self, phone_number: str, code: str, state: str) -> dict[str, str]:
        """Verify SMS via Eventyr `POST /api/auth/complete-signin` → JWT tokens."""
        phone = self.normalize_phone(phone_number)
        otp = self.normalize_otp_code(code)
        if len(otp) < 4:
            raise LoginFailed("Oppgi SMS-koden du mottok")
        if not state:
            raise LoginFailed("OTP-sesjon mangler — start på nytt med mobilnummer")
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
            async with self._session.post(
                f"{API_BASE}{EP_AUTH_COMPLETE_SIGNIN}",
                json=payload,
                headers={
                    "User-Agent": CDN_USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    detail = body.get("detail") if isinstance(body, dict) else body
                    raise LoginFailed(f"SMS-innlogging feilet ({resp.status}): {detail}")
        except (ClientError, TimeoutError) as err:
            raise ProviderUnavailableError(f"Fabel OTP verify failed: {err}") from err

        if not isinstance(body, dict):
            raise LoginFailed("Uventet svar fra Fabel complete-signin")
        access = body.get("access_token") or body.get("accessToken")
        refresh = body.get("refresh_token") or body.get("refreshToken")
        if not access:
            raise LoginFailed(
                f"Ingen access token etter SMS-innlogging (keys={sorted(body.keys())})"
            )
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

    async def get_books_in_progress(self) -> Any:
        """Return books currently in progress for the profile."""
        return await self.request_json("GET", EP_BOOKS_IN_PROGRESS, require_profile=True)

    async def get_favorites(self) -> Any:
        """Return favorite books."""
        return await self.request_json("GET", EP_FAVORITES, require_profile=True)

    async def get_completed(self) -> Any:
        """Return completed books."""
        return await self.request_json("GET", EP_COMPLETED, require_profile=True)

    async def get_progress(self) -> Any:
        """Return listening progress payload."""
        return await self.request_json("GET", EP_PROGRESS, require_profile=True)

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
