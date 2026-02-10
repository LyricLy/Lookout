from __future__ import annotations

import datetime
import hashlib
import logging
import re
import io
from dataclasses import dataclass
from typing import TypedDict, Literal

import aiosqlite
import discord
import gamelogs
from discord.ext import commands

import config
from .bot import Lookout
from .views import File


log = logging.getLogger(__name__)


def gist_of(game: gamelogs.GameResult) -> str:
    return ",".join(f"{player.game_name}/{player.account_name}/{player.ending_ident.role}" for player in game.players)

def datetime_of_filename(filename: str) -> datetime.datetime | None:
    if m := re.fullmatch(r".*-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}).html", filename):
        return datetime.datetime.strptime(m[1], "%Y-%m-%d-%H-%M")

btos2_roles = {"Pacifist", "Banshee", "Warlock", "Inquisitor", "Auditor", "Judge", "Starspawn", "Jackal"}

# close enough, welcome back Go errors
def parse_game(text: str) -> tuple[tuple[gamelogs.GameResult, int] | None, str | None]:
    try:
        return gamelogs.parse(text, gamelogs.ResultAnalyzer() & gamelogs.MessageCountAnalyzer(), clean_tags=False), None
    except gamelogs.InvalidHTMLError:
        return None, "File is not valid HTML"
    except gamelogs.NotLogError:
        return None, "Does not appear to be a gamelog"
    except gamelogs.UnsupportedRoleError as e:
        name = e.args[0]
        return None, f'Unknown role "{name}"{" (BToS2 is not supported)"*(name in btos2_roles)}'


@dataclass
class Gamelog:
    content: str
    filename: str
    url: str | None
    first_upload: datetime.datetime

    def format_upload_time(self) -> str:
        return discord.utils.format_dt(self.first_upload, 'D')

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

    async def fetch_log(self, game: gamelogs.GameResult) -> Gamelog:
        async with self.bot.db.execute(
            "SELECT Best.*, First.message_id as first_message_id FROM Games INNER JOIN Gamelogs AS Best ON Best.hash = from_log INNER JOIN Gamelogs AS First ON First.hash = first_log WHERE gist = ?",
            (gist_of(game),),
        ) as cur:
            r = await cur.fetchone()
        if r is None:
            raise ValueError("game not found")

        if await self.message_exists(r["channel_id"], r["message_id"]):
            url = f"https://cdn.discordapp.com/attachments/{r['channel_id']}/{r['attachment_id']}/{r['filename']}"
        else:
            url = None

        return Gamelog(r["clean_content"], r["filename"], url, discord.utils.snowflake_time(r["first_message_id"]))

    async def see_message(self, message: discord.Message, *, cry: bool = False) -> tuple[int, list[str]]:
        tears = []
        c = 0

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
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO Gamelogs (hash, filename, channel_id, message_id, attachment_id, filename_time, uploader, clean_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (digest, attach.filename, message.channel.id, message.id, attach.id, datetime_of_filename(attach.filename), message.author.id, clean_content),
            )
            await self.bot.db.commit()

            r, error = parse_game(clean_content)
            if error:
                tears.append((attach, error))
            if not r:
                continue

            game, message_count = r

            if game.modifiers != ["Town Traitor"]:
                log.warn(f"non-TT game: {attach.filename}")
                tears.append((attach, "Not a game of Town Traitor"))
                continue
            if any(gamelogs.bucket_of[player.ending_ident.role].startswith("Neutral") for player in game.players):
                tears.append((attach, "Contains neutrals"))
                continue

            for player in game.players:
                await self.bot.db.execute("INSERT OR IGNORE INTO Names VALUES (?, (SELECT COALESCE(MAX(player), 0) + 1 FROM Names))", (player.account_name,))

            gist = gist_of(game)
            row = (gist, digest, message_count, game, gamelogs.version)
            try:
                await self.bot.db.execute("INSERT INTO Games (gist, from_log, first_log, message_count, analysis, analysis_version) VALUES (?1, ?2, ?2, ?3, ?4, ?5)", row)
            except aiosqlite.IntegrityError:
                async with self.bot.db.execute("SELECT message_count FROM Games WHERE gist = ?", (gist,)) as cur:
                    existing_count, = await cur.fetchone()  # type: ignore
                if message_count >= existing_count:
                    await self.bot.db.execute("UPDATE Games SET from_log = ?2, message_count = ?3, analysis = ?4, analysis_version = ?5 WHERE gist = ?1", row)
            else:
                c += 1

            await self.bot.db.execute("UPDATE Gamelogs SET game = ? WHERE hash = ?", (gist, digest))
            await self.bot.db.commit()

        return c, tears

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self.bot.db.execute("SELECT COALESCE(MAX(message_id), 0) FROM Gamelogs") as cur:
            start, = await cur.fetchone()  # type: ignore
        channel = self.bot.get_partial_messageable(config.gamelog_channel_id)
        log.info("catching up")
        c = 0
        async for message in channel.history(limit=None, after=discord.Object(id=start)):
            added, _ = await self.see_message(message)
            c += added
        log.info("caught up on %d games", c)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != config.gamelog_channel_id:
            return
        added, tears = await self.see_message(message)
        log.info("added %d games from %d (issues: %r)", added, message.id, tears)
        if tears:
            await message.channel.send("\n".join(f"- {attach}: {tear}" for attach, tear in tears))

    @commands.command()
    @commands.is_owner()
    async def gamedump(self, ctx: commands.Context) -> None:
        cache = {}
        async with self.bot.db.execute("SELECT filename, clean_content, uploader FROM Gamelogs") as cur:
            async for filename, content, uploader in cur:
                if uploader in cache:
                    name = cache[uploader]
                else:
                    name = (self.bot.get_user(uploader) or await self.bot.fetch_user(uploader)).name
                    cache[uploader] = name
                with open(f"log_area/{name}-{filename}", "w") as f:
                    f.write(content)


async def setup(bot: Lookout):
    await bot.add_cog(Gamelogs(bot))
