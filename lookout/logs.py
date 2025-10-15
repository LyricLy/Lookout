from __future__ import annotations

import logging
import hashlib
from typing import TypedDict, Literal

import discord
import gamelogs
import msgpack
from discord.ext import commands

import config
from .bot import Lookout


log = logging.getLogger(__name__)

def gist_of(game: gamelogs.GameResult) -> str:
    return ",".join(f"{player.game_name}/{player.account_name}/{player.ending_ident.role}" for player in game.players)

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


class Gamelogs(commands.Cog):
    """Gamelog tracking."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

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
                "INSERT OR IGNORE INTO Gamelogs (hash, filename, message_id, attachment_id, uploader, clean_content) VALUES (?, ?, ?, ?, ?, ?)",
                (digest, attach.filename, message.id, attach.id, message.author.id, clean_content),
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

            c += 1
            await self.bot.db.execute(
                "INSERT INTO Games (gist, from_log, message_count, analysis, analysis_version) VALUES (?, ?, ?, ?, ?)"
                "ON CONFLICT (gist) DO UPDATE SET from_log = ?2, message_count = ?3, analysis = ?4, analysis_version = ?5 WHERE excluded.message_count > message_count",
                (gist_of(game), digest, message_count, game, gamelogs.version),
            )
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


async def setup(bot: Lookout):
    await bot.add_cog(Gamelogs(bot))
