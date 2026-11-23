# Fabel for Music Assistant

Unofficial [Music Assistant](https://music-assistant.io) community provider for streaming audiobooks from a [Fabel](https://fabel.no) subscription via the Fuse Eventyr API.

> **Not affiliated with Fabel or Fuse.** This provider uses undocumented private APIs observed from the official app. Use at your own risk. Your Fabel account credentials stay on your Music Assistant server.

## Features

- Login with mobile number + SMS (same flow as the Fabel app), with explicit terms acceptance
- Automatic access-token refresh
- Reports listening sessions so Fabel subscription hours (“Dine timer”) are counted
- Honours plan concurrent-stream limits (`max_concurrent_streams`)
- Library browse (in progress, favorites, completed) with cached/throttled API calls
- Progressive WebM/Opus streaming with seek
- Shows spent / remaining listening time on the provider settings page

## Install (Marketplace)

1. In Music Assistant, enable the **community catalog** (admin).
2. Open **Marketplace** → find **Fabel** → install.
3. Add the provider → accept terms → enter mobile number → SMS code → pick listening profile.

Until the provider is admitted to the catalog, you can load it as a local/dev provider from this repository (`fabel/` folder).

## Status

`alpha` — subscribed audiobook playback with hour reporting; full catalogue search is still limited.

## Development

```bash
uv sync
scripts/run.sh
# open http://localhost:8095
```

Tests: `uv run pytest`

## License

Apache-2.0
