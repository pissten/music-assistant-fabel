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

# Public search-only Algolia config embedded in no.fabel 2.1.6 for catalogue search
ALGOLIA_APP_ID = "DR7EYUET6B"
ALGOLIA_SEARCH_KEY = "f8f99c0505eb71338d94325c88d92a21"
ALGOLIA_BOOKS_INDEX = "prod-reactor-books-v1"
ALGOLIA_SEARCH_URL = (
    f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_BOOKS_INDEX}/query"
)

# Android SMS autofill hash (also accepted by Eventyr initiate-signin)
SMS_APP_HASH = "f8c9a7e21d3"

CONF_ACCESS_TOKEN = "access_token"  # noqa: S105
CONF_REFRESH_TOKEN = "refresh_token"  # noqa: S105
CONF_PROFILE_ID = "profile_id"
CONF_PHONE_NUMBER = "phone_number"
CONF_OTP_CODE = "otp_code"
CONF_OTP_STATE = "otp_state"
CONF_ACCEPT_TERMS = "accept_terms"

HEADER_PROFILE_ID = "x-profile-id"
HEADER_PROFILE_TYPE = "x-profile-type"
HEADER_SUBSCRIPTION_PLAN = "x-subscription-plan"

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
EP_HOME_PAGE = "/home-page"
EP_GENRES_V2 = "/genresV2"
EP_GENRE_BOOKS = "/genres/{genre_id}/books"
EP_BOOK = "/book/{isbn}"
EP_BOOK_PROGRESS = "/books/{isbn}/progress"
EP_BOOK_CAN_LISTEN = "/book/{isbn}/can-listen"
EP_BOOK_STREAM_V2 = "/book/{isbn}/streamV2"
EP_LISTENING_SESSIONS_BATCH = "/listening-sessions/batch"
# App token refresh (NOT OIDC token endpoint) — POST JSON {refreshToken}
EP_AUTH_TOKEN = "/auth/token"  # noqa: S105
# Native app SMS OTP (returns Eventyr JWTs — not Min Konto/Fuse short tokens)
EP_AUTH_INITIATE_SIGNIN = "/auth/initiate-signin"
EP_AUTH_COMPLETE_SIGNIN = "/auth/complete-signin"

# Public legal documents the user must accept before we send legalConsents
FABEL_TERMS_URL = "https://fabel.no/vilkar"
FABEL_PRIVACY_URL = "https://fabel.no/personvern"

# Soft cache for library / browse list endpoints
LIBRARY_CACHE_TTL_SECONDS = 10 * 60
CATALOG_CACHE_TTL_SECONDS = 15 * 60

# Minimum playback delta (seconds) between live progress pushes to Fabel
PROGRESS_SYNC_MIN_DELTA_SECONDS = 15

# Eventyr `/genres/{id}/books` accepts only these pageSize values
VALID_GENRE_PAGE_SIZES = (10, 20, 30, 50, 100)

# Conservative Eventyr rate limit
API_RATE_LIMIT = 3
API_RATE_PERIOD = 1.0

USER_AGENT = "MusicAssistant-Fabel/0.2.0 (compatible; Fabel/2.1.6)"
# Match official app for CDN / auth
CDN_USER_AGENT = "okhttp/4.12.0"
