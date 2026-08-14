This documentation reflects the functionality present in the current source code.

---

## Technical Overview

After the weekly chart run, the bot posts each user's generated chart to the configured Discord channel.

The bot can run as an always-on Discord process for interactive commands. Friday scheduling is
owned by the GitHub Actions workflow by default; set `ENABLE_INTERNAL_SCHEDULER=true` only when
the always-on process should own scheduling instead. Do not enable both schedulers for the same
channel.

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
| `!setchannel` / `/setchannel` | None | Registers the current channel as the destination for weekly updates. |
| `!adduser` / `/adduser` | `<username>` | Adds a Last.fm username to `users.json`. |
| `!removeuser` / `/removeuser` | `<username>` | Removes a persisted Last.fm username. |
| `!users` / `/users` | None | Lists the usernames included in weekly updates. |
| `!runweekly` / `/runweekly` | None | Manually triggers 7-day chart generation for every configured user. |
| `!status` / `/status` | None | Shows the configured channel, user count, last run, and scheduler mode. |
| `!chart` / `/chart` | `<username> [period]` | Generates a chart for one Last.fm account. |

Owner-only maintenance commands:

| Command | Argument | Description |
| --- | --- | --- |
| `!reload` / `/reload` | None | Validates and reloads persisted runtime data. |
| `!clearusers` / `/clearusers` | `[yes]` | Clears persisted users after `yes` confirmation. |

The bot also exposes `/` versions of the commands through Discord slash-command synchronization.

## General Commands

Available to all users.

### `!chart <username> [period]`

Generates a 3x3 chart for a specific Last.fm account.

* **username**: The target Last.fm account name (required).
* **period**: One of `7day` (default), `1month`, `3month`, `6month`, `12month`, or `overall`.

Chart requests have a 30-second per-user cooldown to avoid exhausting the Last.fm API.

### `!about` / `/about`

Shows a short description and command hint.

## Hosting and scheduled updates

For interactive commands, use an always-on worker/service. A once-per-week job alone cannot
support commands such as `!chart` and `!runweekly`.

Run the bot locally or on a worker with:

```bash
uv run fmdiscordbot
```

The scheduled workflow runs at 09:00 America/New_York on Friday. This timezone follows the
workflow's EST/EDT standard and handles daylight saving time correctly.

Required environment variables:

* `DISCORD_TOKEN`
* `LASTFM_API_KEY`
* `LASTFM_API_SECRET`

Optional environment variables:

* `DISCORD_CHANNEL_ID`
* `WEEKLY_USERNAMES`
* `DB_FILE` (defaults to `users.json`)
* `ENABLE_INTERNAL_SCHEDULER` (defaults to `false`)

One-shot command:

```bash
uv run fmdiscordbot --once
```

Use either the GitHub Actions workflow or `ENABLE_INTERNAL_SCHEDULER=true` for Friday
scheduling, not both.
