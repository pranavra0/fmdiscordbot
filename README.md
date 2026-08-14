Discord bot for sharing Last.fm 3x3 album charts with a private friend group.

## Quick start

```bash
uv sync
cp .env.example .env
# Fill in DISCORD_TOKEN, LASTFM_API_KEY, and LASTFM_API_SECRET.
uv run fmdiscordbot
```

The bot supports both prefix commands (`!chart`) and synchronized Discord slash commands
(`/chart`). Friday scheduling is handled by the GitHub Actions workflow by default. Set
`ENABLE_INTERNAL_SCHEDULER=true` only when an always-on bot process should own scheduling.

See [docs.md](docs.md) for configuration, commands, hosting, and scheduler details.