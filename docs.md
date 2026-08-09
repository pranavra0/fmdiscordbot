This documentation reflects the functionality present in the current source code.

---

## Technical Overview

The bot generates 300x300 pixel album tiles in a 3x3 grid (900x900 total). Each tile includes a text overlay displaying the artist name and album title. If an album cover is unavailable, a gray placeholder is rendered behind the text.

After the weekly chart run, the bot posts each user's generated chart to the configured Discord channel.

The bot is designed to run as an always-on Discord process. That allows both manual commands and the automatic Friday post scheduler to work from the same bot instance.

### Development with uv

This project uses [uv](https://docs.astral.sh/uv/) for Python versions, dependency
resolution, virtual environments, and command execution.

```bash
uv sync
uv run fmdiscordbot
```

For local configuration, copy `.env.example` to `.env` and fill in the required
secrets. Runtime state is kept in the ignored `users.json` file.

The repository's checks are:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check src
```


---

## Configuration Commands

These commands require **Administrator** permissions.

| Command | Argument | Description |
| --- | --- | --- |
| `!setchannel` | None | Registers the current channel as the destination for batch weekly updates. |
| `!adduser` | `<username>` | Appends a Last.fm username to `users.json` for inclusion in batch updates. |
| `!runweekly` | None | Manually triggers 7-day chart generation for every user in the database. |
| `!chart` | `<username> [period]` | Generates a chart for one Last.fm account. |

The bot also checks automatically in the `America/Chicago` timezone and posts the weekly chart batch every Friday to the configured channel.

---

## General Commands

Available to all users.

### `!chart <username> [period]`

Generates a 3x3 chart for a specific Last.fm account.

* **username**: The target Last.fm account name (required).
* **period**: The timeframe for the data. Options: `7day` (default), `1month`, `3month`, `6month`, `12month`, `overall`.

---


---
## Hosting and scheduled updates

For the interactive bot, use an always-on worker/service. A once-per-week job
alone cannot support commands such as `!chart` and `!runweekly`.

Run the bot locally or on a worker with:

```bash
uv run fmdiscordbot
```

environment variables:

* `DISCORD_TOKEN`
* `LASTFM_API_KEY`
* `LASTFM_API_SECRET`

one shot command 
```bash
uv run fmdiscordbot --once
```