from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, TypedDict, cast
from zoneinfo import ZoneInfo

import discord
import pylast
import requests
from discord.ext import commands, tasks
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

LOGGER = logging.getLogger(__name__)
BOT_TIMEZONE = ZoneInfo("America/New_York")
VALID_PERIODS = ("7day", "1month", "3month", "6month", "12month", "overall")


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is unavailable or invalid."""


class UserData(TypedDict):
    usernames: list[str]
    config_channel: int | None
    last_auto_run_date: str | None
    last_run: float | None


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    discord_token: str | None
    lastfm_api_key: str | None
    lastfm_api_secret: str | None
    db_file: Path
    channel_id: int | None
    usernames: tuple[str, ...]
    enable_internal_scheduler: bool

    @classmethod
    def from_environment(cls) -> Settings:
        channel_value = os.getenv("DISCORD_CHANNEL_ID")
        try:
            channel_id = int(channel_value) if channel_value else None
        except ValueError as error:
            raise ConfigurationError("DISCORD_CHANNEL_ID must be an integer.") from error

        usernames = tuple(
            username.strip()
            for username in os.getenv("WEEKLY_USERNAMES", "").split(",")
            if username.strip()
        )
        scheduler_value = os.getenv("ENABLE_INTERNAL_SCHEDULER", "false").strip().lower()
        if scheduler_value not in {"true", "false"}:
            raise ConfigurationError("ENABLE_INTERNAL_SCHEDULER must be true or false.")
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN"),
            lastfm_api_key=os.getenv("LASTFM_API_KEY"),
            lastfm_api_secret=os.getenv("LASTFM_API_SECRET"),
            db_file=Path(os.getenv("DB_FILE") or "users.json"),
            channel_id=channel_id,
            usernames=usernames,
            enable_internal_scheduler=scheduler_value == "true",
        )

    def validate(self) -> None:
        required = {
            "DISCORD_TOKEN": self.discord_token,
            "LASTFM_API_KEY": self.lastfm_api_key,
            "LASTFM_API_SECRET": self.lastfm_api_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(
                f"Missing required environment variables: {', '.join(missing)}"
            )


SETTINGS = Settings.from_environment()
NETWORK: pylast.LastFMNetwork | None = None
RUN_ONCE = False
RUN_ONCE_FAILED = False
COMMANDS_SYNCED = False
WEEKLY_UPDATE_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def _lastfm_network() -> pylast.LastFMNetwork:
    if NETWORK is None:
        raise ConfigurationError("Last.fm network has not been initialized.")
    return NETWORK


def _empty_user_data() -> UserData:
    return {
        "usernames": [],
        "config_channel": None,
        "last_auto_run_date": None,
        "last_run": None,
    }


def load_data() -> UserData:
    if not SETTINGS.db_file.exists():
        return _empty_user_data()

    try:
        with SETTINGS.db_file.open(encoding="utf-8") as file:
            raw_data: Any = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Could not read {SETTINGS.db_file}: {error}") from error

    if not isinstance(raw_data, dict):
        raise ConfigurationError(f"{SETTINGS.db_file} must contain a JSON object.")

    usernames = raw_data.get("usernames", [])
    if not isinstance(usernames, list) or not all(
        isinstance(username, str) for username in usernames
    ):
        raise ConfigurationError(f"{SETTINGS.db_file}.usernames must be a list of strings.")

    config_channel = raw_data.get("config_channel")
    if config_channel is not None and not isinstance(config_channel, int):
        raise ConfigurationError(f"{SETTINGS.db_file}.config_channel must be an integer.")

    last_auto_run_date = raw_data.get("last_auto_run_date")
    if last_auto_run_date is not None and not isinstance(last_auto_run_date, str):
        raise ConfigurationError(f"{SETTINGS.db_file}.last_auto_run_date must be a string.")

    last_run = raw_data.get("last_run")
    if last_run is not None and not isinstance(last_run, (int, float)):
        raise ConfigurationError(f"{SETTINGS.db_file}.last_run must be numeric.")

    return {
        "usernames": usernames,
        "config_channel": config_channel,
        "last_auto_run_date": last_auto_run_date,
        "last_run": float(last_run) if last_run is not None else None,
    }


def save_data(data: UserData) -> None:
    try:
        SETTINGS.db_file.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = SETTINGS.db_file.with_suffix(f"{SETTINGS.db_file.suffix}.tmp")
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)
            file.write("\n")
        temporary_file.replace(SETTINGS.db_file)
    except OSError as error:
        raise ConfigurationError(f"Could not write {SETTINGS.db_file}: {error}") from error


async def safe_send(
    channel: discord.abc.Messageable,
    content: str | None = None,
    file: discord.File | None = None,
    max_retries: int = 3,
) -> discord.Message:
    for attempt in range(max_retries):
        try:
            if file is None:
                return await channel.send(content)
            return await channel.send(content, file=file)
        except (discord.HTTPException, ConnectionError, OSError) as error:
            if attempt == max_retries - 1:
                raise
            wait = 2**attempt
            LOGGER.warning(
                "Send failed (attempt %d/%d): %s. Retrying in %ds.",
                attempt + 1,
                max_retries,
                error,
                wait,
            )
            await asyncio.sleep(wait)


def _chart_filename(username: str, period: str) -> str:
    safe_username = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in username
    )[:64]
    return f"{safe_username or 'chart'}_{period}.png"


def _effective_usernames(data: UserData) -> list[str]:
    return data["usernames"] or list(SETTINGS.usernames)


def _format_last_run(timestamp: float | None) -> str:
    if timestamp is None:
        return "never"
    return datetime.fromtimestamp(timestamp, BOT_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")


async def send_weekly_update(channel: discord.abc.Messageable) -> tuple[bool, str | None]:
    async with WEEKLY_UPDATE_LOCK:
        data = load_data()
        usernames = data["usernames"] or list(SETTINGS.usernames)

        if not usernames:
            return False, "User list is empty. Add users with !adduser or WEEKLY_USERNAMES."

        for username in usernames:
            img_buffer = await asyncio.get_running_loop().run_in_executor(
                None, get_chart_image, username, "7day"
            )

            if img_buffer:
                file = discord.File(fp=img_buffer, filename=_chart_filename(username, "7day"))
                await safe_send(channel, content=f"Weekly 7-day chart for {username}", file=file)
            else:
                await safe_send(
                    channel, content=f"Could not generate a 7-day chart for {username}."
                )

            await asyncio.sleep(2)

        return True, None


# --- IMAGE GENERATION LOGIC ---
def get_chart_image(
    username: str,
    period: str = "7day",
    rows: int = 3,
    cols: int = 3,
) -> BytesIO | None:
    if rows < 1 or cols < 1:
        raise ValueError("rows and cols must be positive integers.")

    try:
        user = _lastfm_network().get_user(username)
        top_albums = user.get_top_albums(period=period, limit=rows * cols)

        if not top_albums:
            return None

        img_size = 300
        final_image = Image.new("RGB", (cols * img_size, rows * img_size), color=(20, 20, 20))
        draw = ImageDraw.Draw(final_image)

        try:
            font = ImageFont.truetype("arial.ttf", 16)
            bold_font = ImageFont.truetype("arial.ttf", 18)
        except OSError:
            font = ImageFont.load_default()
            bold_font = ImageFont.load_default()

        for index, item in enumerate(top_albums):
            x = (index % cols) * img_size
            y = (index // cols) * img_size
            album: Any = item.item
            artist_name = str(album.artist.name)
            album_name = str(album.title)
            cover_url = album.get_cover_image(pylast.SIZE_EXTRA_LARGE)

            has_image = False
            if cover_url:
                try:
                    response = requests.get(cover_url, timeout=5)
                    response.raise_for_status()
                    with Image.open(BytesIO(response.content)) as image:
                        final_image.paste(
                            image.convert("RGB").resize((img_size, img_size)),
                            (x, y),
                        )
                    has_image = True
                except (OSError, requests.RequestException, ValueError) as error:
                    LOGGER.warning("Could not load album art for %s: %s", album_name, error)

            if not has_image:
                draw.rectangle([x, y, x + img_size, y + img_size], fill=(40, 40, 40))

            overlay = Image.new("RGBA", (img_size, 60), (0, 0, 0, 160))
            final_image.paste(overlay, (x, y + img_size - 60), overlay)

            artist_text = textwrap.shorten(artist_name, width=30, placeholder="...")
            album_text = textwrap.shorten(album_name, width=35, placeholder="...")
            draw.text((x + 10, y + img_size - 50), artist_text, font=bold_font, fill="white")
            draw.text((x + 10, y + img_size - 25), album_text, font=font, fill="lightgray")

        buffer = BytesIO()
        final_image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    except Exception:
        LOGGER.exception("Error generating chart for %s", username)
        return None


# --- BOT COMMANDS ---


async def _configured_channel(channel_id: int | None) -> discord.abc.Messageable | None:
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden) as error:
            LOGGER.error("Could not fetch configured channel %d: %s", channel_id, error)
            return None
    if isinstance(channel, discord.abc.Messageable):
        return cast(discord.abc.Messageable, channel)
    LOGGER.error("Configured channel %d cannot receive messages.", channel_id)
    return None


async def _run_once() -> None:
    data = load_data()
    channel_id = data["config_channel"] or SETTINGS.channel_id
    channel = await _configured_channel(channel_id)
    if channel is None:
        raise ConfigurationError(
            "No Discord channel configured. Set DISCORD_CHANNEL_ID or use !setchannel."
        )

    success, error_message = await send_weekly_update(channel)
    if not success:
        raise ConfigurationError(error_message or "Weekly update did not complete.")

    data["last_auto_run_date"] = datetime.now(BOT_TIMEZONE).date().isoformat()
    data["last_run"] = datetime.now(BOT_TIMEZONE).timestamp()
    save_data(data)
    LOGGER.info("One-shot weekly update completed.")


@bot.event
async def on_ready() -> None:
    global COMMANDS_SYNCED, RUN_ONCE_FAILED

    LOGGER.info("Logged in as %s.", bot.user)
    if not COMMANDS_SYNCED:
        try:
            await bot.tree.sync()
            COMMANDS_SYNCED = True
            LOGGER.info("Slash commands synchronized.")
        except discord.DiscordException:
            LOGGER.exception("Could not synchronize slash commands.")

    if RUN_ONCE:
        try:
            await _run_once()
        except (ConfigurationError, discord.DiscordException):
            RUN_ONCE_FAILED = True
            LOGGER.exception("One-shot weekly update failed.")
        finally:
            await bot.close()
        return

    if SETTINGS.enable_internal_scheduler and not auto_weekly_runner.is_running():
        auto_weekly_runner.start()
    elif not SETTINGS.enable_internal_scheduler:
        LOGGER.info("Internal scheduler disabled; use the weekly-update workflow.")


@tasks.loop(minutes=30)
async def auto_weekly_runner() -> None:
    now = datetime.now(BOT_TIMEZONE)
    if now.weekday() != 4:
        return

    data = load_data()
    today = now.date().isoformat()
    if data["last_auto_run_date"] == today:
        return

    channel = await _configured_channel(data["config_channel"] or SETTINGS.channel_id)
    if channel is None:
        return

    try:
        success, error_message = await send_weekly_update(channel)
    except (ConfigurationError, discord.DiscordException):
        LOGGER.exception("Automatic weekly update failed.")
        return

    if success:
        data["last_auto_run_date"] = today
        data["last_run"] = datetime.now(BOT_TIMEZONE).timestamp()
        save_data(data)
        LOGGER.info("Automatic weekly update completed for %s.", today)
    else:
        LOGGER.warning("Automatic weekly update skipped: %s", error_message)


@auto_weekly_runner.before_loop
async def before_auto_weekly_runner() -> None:
    await bot.wait_until_ready()


@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def runweekly(ctx: commands.Context[Any]) -> None:
    """Manually trigger the weekly update for all configured users."""
    data = load_data()
    channel = await _configured_channel(data["config_channel"] or SETTINGS.channel_id)
    if channel is None:
        await ctx.send("No channel set. Use !setchannel first.")
        return

    await ctx.send("Starting weekly generation.")
    try:
        success, error_message = await send_weekly_update(channel)
    except (ConfigurationError, discord.DiscordException):
        LOGGER.exception("Manual weekly update failed.")
        await ctx.send("Weekly generation failed. Check the bot logs.")
        return

    if not success:
        await ctx.send(error_message or "Weekly generation did not complete.")
        return

    data["last_run"] = datetime.now(BOT_TIMEZONE).timestamp()
    save_data(data)
    await ctx.send("Weekly update complete.")


@bot.hybrid_command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def chart(
    ctx: Any,  # noqa: ANN401 - discord.py hybrid-command typing is incomplete
    username: str | None = None,
    period: Literal["7day", "1month", "3month", "6month", "12month", "overall"] = "7day",
) -> None:
    """Request a chart for a Last.fm username."""
    if not username:
        await ctx.send("Provide a username.")
        return
    if period not in VALID_PERIODS:
        await ctx.send(f"Invalid period. Choose one of: {', '.join(VALID_PERIODS)}.")
        return

    await ctx.send(f"Generating {period} chart for {username}.")
    img_buffer = await asyncio.get_running_loop().run_in_executor(
        None, get_chart_image, username, period
    )
    if img_buffer:
        await ctx.send(file=discord.File(fp=img_buffer, filename=_chart_filename(username, period)))
    else:
        await ctx.send(f"Error retrieving the {period} chart for {username}.")


@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def adduser(ctx: Any, username: str | None = None) -> None:  # noqa: ANN401
    """Add a Last.fm username to the weekly update list."""
    if not username:
        await ctx.send("Provide a Last.fm username.")
        return
    username = username.strip()

    data = load_data()
    if any(existing.casefold() == username.casefold() for existing in data["usernames"]):
        await ctx.send("User already exists.")
        return
    if not data["usernames"]:
        data["usernames"] = _effective_usernames(data)
    if any(existing.casefold() == username.casefold() for existing in data["usernames"]):
        await ctx.send("User already exists.")
        return
    data["usernames"].append(username)
    save_data(data)
    await ctx.send(f"Added {username}.")


@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx: Any, username: str | None = None) -> None:  # noqa: ANN401
    """Remove a Last.fm username from the weekly update list."""
    if not username:
        await ctx.send("Provide a Last.fm username.")
        return
    username = username.strip()
    data = load_data()
    for index, existing in enumerate(data["usernames"]):
        if existing.casefold() == username.casefold():
            data["usernames"].pop(index)
            save_data(data)
            await ctx.send(f"Removed {existing}.")
            return
    if not data["usernames"] and SETTINGS.usernames:
        await ctx.send("These users come from WEEKLY_USERNAMES; remove them from the environment.")
    else:
        await ctx.send("User is not configured.")


@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def users(ctx: commands.Context[Any]) -> None:
    """List Last.fm usernames included in weekly updates."""
    data = load_data()
    usernames = _effective_usernames(data)
    if not usernames:
        await ctx.send("No users configured.")
        return
    source = "users.json" if data["usernames"] else "WEEKLY_USERNAMES"
    listing = "\n".join(f"{index}. {username}" for index, username in enumerate(usernames, 1))
    await ctx.send(f"Weekly users ({source}):\n{listing}")


@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def status(ctx: commands.Context[Any]) -> None:
    """Show bot configuration and the last successful weekly run."""
    data = load_data()
    channel_id = data["config_channel"] or SETTINGS.channel_id
    channel = f"<#{channel_id}>" if channel_id else "not configured"
    scheduler = "enabled" if SETTINGS.enable_internal_scheduler else "workflow-only"
    await ctx.send(
        f"Channel: {channel}\n"
        f"Weekly users: {len(_effective_usernames(data))}\n"
        f"Last successful run: {_format_last_run(data['last_run'])}\n"
        f"Scheduler: {scheduler} ({BOT_TIMEZONE.key})"
    )


@bot.hybrid_command()
async def about(ctx: commands.Context[Any]) -> None:
    """Show a short description of the bot."""
    await ctx.send(
        "Last.fm 3x3 chart bot. Use `!chart <username> [period]` or `/chart` to generate a chart."
    )


@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx: commands.Context[Any]) -> None:
    """Set the current channel as the weekly update destination."""
    data = load_data()
    data["config_channel"] = ctx.channel.id
    save_data(data)
    await ctx.send(f"Weekly updates will be posted in <#{ctx.channel.id}>.")


@bot.hybrid_command()
@commands.is_owner()
async def reload(ctx: commands.Context[Any]) -> None:
    """Validate and reload persisted runtime data."""
    data = load_data()
    await ctx.send(f"Reloaded runtime data: {len(_effective_usernames(data))} weekly users.")


@bot.hybrid_command()
@commands.is_owner()
async def clearusers(ctx: commands.Context[Any], confirmation: str = "") -> None:
    """Clear persisted weekly users after an explicit confirmation."""
    if confirmation.casefold() != "yes":
        await ctx.send("This removes persisted users. Run `!clearusers yes` to confirm.")
        return
    data = load_data()
    data["usernames"] = []
    save_data(data)
    await ctx.send("Persisted weekly users cleared.")


@bot.event
async def on_command_error(ctx: commands.Context[Any], error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Try again in {error.retry_after:.0f} seconds.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Administrator permission for that command.")
        return
    if isinstance(error, commands.NotOwner):
        await ctx.send("Only the bot owner can use that command.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`.")
        return
    LOGGER.error("Unhandled command error: %s", error)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Discord bot, optionally as a one-shot weekly worker."""
    global NETWORK, RUN_ONCE, RUN_ONCE_FAILED, SETTINGS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one weekly update and exit instead of staying connected.",
    )
    args = parser.parse_args(argv)
    SETTINGS = Settings.from_environment()
    try:
        SETTINGS.validate()
    except ConfigurationError as error:
        LOGGER.error("%s", error)
        return 2

    NETWORK = pylast.LastFMNetwork(
        api_key=SETTINGS.lastfm_api_key or "",
        api_secret=SETTINGS.lastfm_api_secret or "",
    )
    RUN_ONCE = args.once
    RUN_ONCE_FAILED = False
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        bot.run(SETTINGS.discord_token or "")
    except (discord.LoginFailure, discord.PrivilegedIntentsRequired, OSError):
        LOGGER.exception("Discord bot stopped because startup failed.")
        return 1
    if RUN_ONCE and RUN_ONCE_FAILED:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
