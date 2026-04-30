import io

import discord
from discord.ext import commands

from .bot import *
from .player_info import PlayerInfo
from .logs import Gamelogs
from .stats import Stats
from .views import ConfirmationView

import config


class Admin(commands.Cog):
    """Bot/database administration for game hosts."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot
        self.logs = bot.require_cog(Gamelogs)
        self.stats = bot.require_cog(Stats)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True

        res = discord.utils.find(lambda p: p[1], [(g, g.get_role(config.game_host_id)) for g in self.bot.guilds])
        if not res:
            return False

        guild, role = res
        member = guild.get_member(ctx.author.id)
        if not member:
            return False

        return role in member.roles

    @commands.command()
    @commands.is_owner()
    @needs_db
    async def gamedump(self, conn: Connection, ctx: Context) -> None:
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

    @commands.command()
    @commands.is_owner()
    @needs_db
    async def bldump(self, conn: Connection, ctx: Context, target: discord.ForumChannel) -> None:
        """Write the blacklist to a target forum channel."""
        if target.id == config.channel_id:
            await ctx.send("It's not a good idea to dump into the current blacklist channel.")
            return

        for thread, reason in await conn.fetchall("SELECT DISTINCT thread_id, reason FROM Blacklists"):
            names = [x for x, in await conn.fetchall("SELECT account_name FROM Blacklists WHERE thread_id = ?", (thread,))]
            files = await conn.fetchall(
                "SELECT filename, clean_content FROM BlacklistGames INNER JOIN Gamelogs ON hash = from_log INNER JOIN Games ON BlacklistGames.gist = Games.gist WHERE thread_id = ?",
                (thread,),
            )
            await target.create_thread(name=", ".join(names), content=reason, files=[discord.File(io.BytesIO(content.encode()), filename=filename) for filename, content in files])

    @commands.command(name="is")
    @commands.is_owner()
    @needs_db
    async def is_(self, conn: Connection, ctx: Context, a: PlayerInfo, b: PlayerInfo) -> None:
        """Treat 2 players as being the same from now on in statistics."""
        if a.id == b.id:
            await ctx.send("I know.")
            return

        conflicts = await conn.fetchall("SELECT game FROM Appearances AS A1 INNER JOIN Appearances AS A2 USING (game) WHERE A1.player = ? AND A2.player = ?", (a.id, b.id))
        if conflicts:
            urls = [f"- {u}" for conflict, in conflicts if (u := await (await self.logs.fetch_log_with_gist(conflict)).url())] if len(conflicts) <= 10 else []
            await ctx.send(f"Refusing to merge players who have appeared together in {len(conflicts)} games.\n{'\n'.join(urls)}")
            return

        timecode, = await conn.fetchone("SELECT MIN(timecode) FROM Appearances WHERE player = ?", (b.id,))
        if timecode:
            await conn.execute("UPDATE Globals SET generation = generation + 1")
            await conn.execute("UPDATE Games SET generation = generation + 1 FROM Gamelogs WHERE first_log = hash AND timecode < ?", (timecode,))

        await conn.execute("UPDATE OR IGNORE DiscordConnections SET player = ? WHERE player = ?", (a.id, b.id))
        await conn.execute("UPDATE Names SET player = ? WHERE player = ?", (a.id, b.id))
        await conn.execute("DELETE FROM Appearances WHERE player = ?", (b.id,))
        await ctx.send(":+1:")

        self.stats.run_games()

    @commands.command()
    @needs_db
    async def cheated(self, conn: Connection, ctx: Context, player: PlayerInfo) -> None:
        """Mark a player as having cheated."""
        await conn.execute("INSERT OR REPLACE INTO Hidden (player, why) VALUES (?, 'cheated')", (player.id,))
        await ctx.send(":+1:")

    @commands.command()
    @needs_db
    async def uncheated(self, conn: Connection, ctx: Context, player: PlayerInfo) -> None:
        """Revert the effect of `cheated`."""
        await conn.execute("DELETE FROM Hidden WHERE player = ?", (player.id,))
        await ctx.send(":+1:")

    @commands.command()
    @needs_db
    async def connect(self, conn: Connection, ctx: Context, who: discord.Member, *, player: PlayerInfo | str) -> None:
        """Associate a player with their Discord account."""
        if isinstance(player, PlayerInfo):
            player_id = player.id
        else:
            view = ConfirmationView(ctx.author)
            view.message = await ctx.send(f"I don't know who that is. Really connect to '{player}'?", view=view)
            if not await view.wait():
                return

            player_id, = await conn.fetchone("INSERT INTO Names VALUES (?, (SELECT COALESCE(MAX(player), 0) + 1 FROM Names)) RETURNING player", (player,))

        await conn.execute("INSERT OR REPLACE INTO DiscordConnections (discord_id, player) VALUES (?, ?)", (who.id, player_id))
        await ctx.send(":+1:")

    @commands.command()
    @needs_db
    async def unconnected(self, conn: Connection, ctx: Context, *, guild: discord.Guild = commands.CurrentGuild) -> None:
        """List Discord members not associated with a ToS2 username."""
        members = []
        for member in guild.members:
            if member.bot:
                continue
            exists = await conn.fetchone("SELECT 1 FROM DiscordConnections WHERE discord_id = ?", (member.id,))
            if not exists:
                members.append(member)
        await ctx.send("\n".join(f"- {member.mention}" for member in members))


async def setup(bot: Lookout):
    await bot.add_cog(Admin(bot))
