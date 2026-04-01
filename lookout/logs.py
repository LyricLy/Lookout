import datetime
import hashlib
import logging
import re
import sqlite3
import io
from dataclasses import dataclass
from typing import Iterator, Self

import discord
import gamelogs
from discord.ext import commands

import config
from .bot import *
from .timecode import Timecode
from .views import File


log = logging.getLogger(__name__)


class NotAGameError(Exception):
    pass


def gist_of(game: gamelogs.GameResult) -> str:
    return ",".join(f"{player.game_name}/{player.account_name}/{player.ending_ident.role.name}" for player in game.players)

def datetime_of_filename(filename: str) -> datetime.datetime | None:
    if m := re.fullmatch(r"(?:.*-)?(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}).*\.html", filename):
        return datetime.datetime.strptime(m[1], "%Y-%m-%d-%H-%M")

btos2_roles = {"Pacifist", "Banshee", "Warlock", "Inquisitor", "Auditor", "Judge", "Starspawn", "Jackal"}

def parse_game(text: str, *, pandora: bool = False) -> tuple[gamelogs.GameResult, int]:
    try:
        return gamelogs.parse(text, gamelogs.ResultAnalyzer(pandora=pandora) & gamelogs.MessageCountAnalyzer(), clean_tags=False)
    except gamelogs.InvalidHTMLError:
        raise NotAGameError("File is not valid HTML")
    except gamelogs.NotLogError:
        raise NotAGameError("Does not appear to be a gamelog")
    except gamelogs.UnsupportedRoleError as e:
        name = e.args[0]
        raise NotAGameError(f'Unknown role "{name}"{" (BToS2 is not supported)"*(name in btos2_roles)}')


@dataclass
class Gamelog:
    content: str
    filename: str
    url: str | None
    first_upload: Timecode

    def format_upload_time(self) -> str:
        return discord.utils.format_dt(self.first_upload.to_datetime(), 'D')

    def to_item(self) -> discord.ui.Item:
        if self.url is None:
            return File(discord.File(io.BytesIO(self.content.encode()), filename=self.filename))
        else:
            return discord.ui.TextDisplay(self.url)


class Gamelogs(commands.Cog):
    """Gamelog tracking."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    async def message_exists(self, channel_id: int, message_id: int) -> bool:
        if (a := config.message_exists(channel_id, message_id)) is not None:
            return a

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False

        try:
            await channel.fetch_message(message_id)
        except discord.NotFound:
            return False

        return True

    @needs_db
    async def fetch_log(self, conn: Connection, game: gamelogs.GameResult) -> Gamelog:
        r = await conn.fetchone("""
            SELECT
                Best.channel_id, Best.message_id, Best.attachment_id, Best.filename, Best.clean_content,
                First.message_id, First.filename_time
            FROM Games
            INNER JOIN Gamelogs AS Best ON Best.hash = from_log
            INNER JOIN Gamelogs AS First ON First.hash = first_log
            WHERE gist = ?
        """, (gist_of(game),))
        if r is None:
            raise ValueError("game not found")

        channel_id, message_id, attachment_id, filename, content, *timecode = r
        if await self.message_exists(channel_id, message_id):
            url = f"https://cdn.discordapp.com/attachments/{channel_id}/{attachment_id}/{filename}"
        else:
            url = None

        return Gamelog(content, filename, url, Timecode(*timecode))

    async def see_log(self, conn: Connection, digest: str, clean_content: str, *, pandora: bool = False, force: bool = False) -> bool:
        game, message_count = parse_game(clean_content, pandora=pandora)

        if game.modifiers != ["Town Traitor"]:
            raise NotAGameError("Not a game of Town Traitor")
        if any(player.ending_ident.faction not in (gamelogs.town, gamelogs.coven) for player in game.players):
            raise NotAGameError("Contains neutrals")

        for player in game.players:
            await conn.execute("INSERT OR IGNORE INTO Names VALUES (?, (SELECT COALESCE(MAX(player), 0) + 1 FROM Names))", (player.account_name,))

        gist = gist_of(game)
        row = (gist, digest, message_count, game, gamelogs.version, game.victor, bool(game.hunt_reached))

        try:
            await conn.execute("INSERT INTO Games (gist, from_log, first_log, message_count, analysis, analysis_version, victor, hunt_reached) VALUES (?1, ?2, ?2, ?3, ?4, ?5, ?6, ?7)", row)
        except sqlite3.IntegrityError:
            existing_count, = await (await self.bot.db.execute("SELECT message_count FROM Games WHERE gist = ?", (gist,))).fetchone()  # type: ignore
            if force or message_count >= existing_count:
                await conn.execute("UPDATE Games SET from_log = ?2, message_count = ?3, analysis = ?4, analysis_version = ?5, victor = ?6, hunt_reached = ?7 WHERE gist = ?1", row)
            return False
        else:
            return True
        finally:
            await conn.execute("UPDATE Gamelogs SET game = ? WHERE hash = ?", (gist, digest))

    @needs_db
    async def see_message(self, conn: Connection, message: discord.Message, *, cry: bool = False) -> tuple[int, list[str]]:
        c = 0
        tears = []

        for attach in message.attachments:
            if not attach.filename.endswith(".html"):
                continue

            try:
                content = (await attach.read()).decode()
            except UnicodeDecodeError:
                continue

            clean_content = gamelogs.clean_tos2_tags(content)
            if not clean_content:
                continue

            digest = hashlib.sha256(clean_content.encode()).hexdigest()
            filename_time = datetime_of_filename(attach.filename)
            await conn.execute(
                "INSERT OR IGNORE INTO Gamelogs (hash, filename, channel_id, message_id, attachment_id, filename_time, uploader, clean_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (digest, attach.filename, message.channel.id, message.id, attach.id, filename_time, message.author.id, clean_content),
            )

            if not filename_time:
                tears.append((attach, "Filename does not contain date and time"))
                continue

            try:
                c += await self.see_log(conn, digest, clean_content, pandora="!pandora" in message.content, force="!force" in message.content)
            except NotAGameError as e:
                tears.append((attach, str(e)))

        return c, tears

    @commands.Cog.listener()
    @needs_db
    async def on_ready(self, conn: Connection) -> None:
        start, = await conn.fetchone("SELECT COALESCE(MAX(message_id), 0) FROM Gamelogs")  # type: ignore
        channel = self.bot.get_partial_messageable(config.gamelog_channel_id)
        log.info("catching up")
        c = 0
        async for message in channel.history(limit=None, after=discord.Object(id=start)):
            added, _ = await self.see_message(message)
            c += added
        if c:
            self.bot.dispatch("saw_games")
        log.info("caught up on %d games", c)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != config.gamelog_channel_id:
            return
        added, tears = await self.see_message(message)
        log.info("added %d games from %d (issues: %r)", added, message.id, tears)
        if added:
            self.bot.dispatch("saw_games")
        if tears:
            await message.channel.send("\n".join(f"- {attach}: {tear}" for attach, tear in tears))

    @commands.command()
    @commands.is_owner()
    @needs_db
    async def gamedump(self, conn: Connection, ctx: commands.Context) -> None:
        """Dump logs from the database into a folder."""
        cache = {}
        for filename, content, uploader in await conn.fetchall("SELECT filename, clean_content, uploader FROM Gamelogs"):
            if uploader in cache:
                name = cache[uploader]
            else:
                name = (self.bot.get_user(uploader) or await self.bot.fetch_user(uploader)).name
                cache[uploader] = name
            with open(f"log_area/{name}-{filename}", "w") as f:
                f.write(content)


async def setup(bot: Lookout):
    await bot.add_cog(Gamelogs(bot))
