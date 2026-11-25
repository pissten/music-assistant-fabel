# Fabel for Music Assistant

Unofficial [Music Assistant](https://music-assistant.io) community provider for streaming audiobooks from a [Fabel](https://fabel.no) subscription via the Fuse Eventyr API.

> **Not affiliated with Fabel or Fuse.** This provider uses undocumented private APIs observed from the official app. Use at your own risk. Your Fabel account credentials stay on your Music Assistant server.

## Features

- Login with mobile number + SMS (same flow as the Fabel app), with explicit terms acceptance
- Automatic access-token refresh (serialized for concurrent requests)
- Full Fabel catalogue search (profile-aware for child and adult-only subscriptions), with cached-library fallback
- Browse your library (**In progress**, **Favorites**, **Completed**) as well as curated **Discover** lists and **Genres**
- Add and remove audiobooks from your Fabel favorites directly from Music Assistant
- Reports listening sessions so Fabel subscription hours (“Dine timer”) are counted
- Two-way listening progress sync (imports resume position, completion state, and timestamps from Fabel, and pushes progress / completion back to the Fabel app)
- Honours plan concurrent-stream limits (`max_concurrent_streams`)
- Progressive WebM/Opus streaming with seek
- Shows spent / remaining listening time on the provider settings page
- Localized UI in English and Norwegian Bokmål (`nb`)

## Install (Marketplace)

1. In Music Assistant, enable the **community catalog** (admin).
2. Open **Marketplace** → find **Fabel** → install.
3. Add the provider → accept terms → enter mobile number → SMS code → pick listening profile.

## Status

`beta` — full catalogue search, Discover & Genres browsing, favorite management, progressive WebM/Opus playback with seek, hour reporting, and two-way progress sync are implemented and tested against live Fabel accounts.

## Development

```bash
uv sync
scripts/run.sh
# open http://localhost:8095
```

Tests: `uv run pytest`

## License

Apache-2.0
