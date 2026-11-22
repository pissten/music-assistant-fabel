# Fabel for Music Assistant

Unofficial [Music Assistant](https://music-assistant.io) community provider for streaming audiobooks from a [Fabel](https://fabel.no) subscription via the Fuse Eventyr API.

> **Not affiliated with Fabel or Fuse.** This provider uses undocumented private APIs observed from the official app. Use at your own risk. Your Fabel account credentials stay on your Music Assistant server.

## Features

- Login with mobile number + SMS (same flow as the Fabel app)
- Automatic access-token refresh
- Library browse (in progress, favorites, completed)
- Progressive WebM/Opus streaming with seek

## Install (Marketplace)

1. In Music Assistant, enable the **community catalog** (admin).
2. Open **Marketplace** → find **Fabel** → install.
3. Add the provider → enter mobile number → SMS code → pick listening profile.

Until the provider is admitted to the catalog, you can load it as a local/dev provider from this repository (`fabel/` folder).

## Status

`alpha` — works for subscribed audiobook playback; catalogue search and bidirectional progress sync are limited.

## Development

```bash
uv sync
scripts/run.sh
# open http://localhost:8095
```

Tests: `uv run pytest`

## License

Apache-2.0
