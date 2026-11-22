"""Constants for the Fabel Music Assistant provider."""

from __future__ import annotations

DOMAIN = "fabel"

# Eventyr / Fuse API (observed in no.fabel 2.1.6 + live traffic)
API_BASE = "https://eventyr.fuseapps.no/api"
CDN_HOST = "fabel-cdn.fuseapps.no"
CDN_BASE = "https://fabel-cdn.fuseapps.no"
OIDC_ISSUER = "https://eventyr.fuseapps.no"
OIDC_DISCOVERY = f"{OIDC_ISSUER}/.well-known/openid-configuration"
# Registered public client (redirect URI not yet confirmed for MA browser login)
OIDC_CLIENT_ID = "fabel_app"

# Android SMS autofill hash (also accepted by Eventyr initiate-signin)
SMS_APP_HASH = "f8c9a7e21d3"

CONF_ACCESS_TOKEN = "access_token"  # noqa: S105
CONF_REFRESH_TOKEN = "refresh_token"  # noqa: S105
CONF_PROFILE_ID = "profile_id"
CONF_PHONE_NUMBER = "phone_number"
CONF_OTP_CODE = "otp_code"
CONF_OTP_STATE = "otp_state"

HEADER_PROFILE_ID = "x-profile-id"

# Confirmed Eventyr API paths
EP_ME = "/me"
EP_PROFILES = "/me/profiles"
EP_PROFILES_CURRENT = "/me/profiles/current"
EP_BOOKS_IN_PROGRESS = "/me/profile/books-in-progress"
EP_PROGRESS = "/me/profile/progress"
EP_FAVORITES = "/me/profile/favorites"
EP_COMPLETED = "/me/profile/completed"
EP_ALLOWANCE = "/me/allowance"
EP_BOOKSHELVES = "/bookshelves"
EP_BOOK = "/book/{isbn}"
EP_BOOK_CAN_LISTEN = "/book/{isbn}/can-listen"
EP_BOOK_STREAM_V2 = "/book/{isbn}/streamV2"
# App token refresh (NOT OIDC token endpoint) — POST JSON {refreshToken}
EP_AUTH_TOKEN = "/auth/token"  # noqa: S105
# Native app SMS OTP (returns Eventyr JWTs — not Min Konto/Fuse short tokens)
EP_AUTH_INITIATE_SIGNIN = "/auth/initiate-signin"
EP_AUTH_COMPLETE_SIGNIN = "/auth/complete-signin"

USER_AGENT = "MusicAssistant-Fabel/0.1.4 (compatible; Fabel/2.1.6)"
# Match official app for CDN / auth
CDN_USER_AGENT = "okhttp/4.12.0"
