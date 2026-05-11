import io

import discord
from discord.ext import commands

from .bot import *
from .db import ser_game_result
from .player_info import PlayerInfo
from .logs import Gamelogs, Timecode
from .stats import Stats
from .views import ConfirmationView, ViewContainer, File

import config


class ViewGamePanel(ViewContainer):
    header = discord.ui.TextDisplay("## Game data")
    display = discord.ui.TextDisplay("")
    sep = discord.ui.Separator()
    management_header = discord.ui.TextDisplay("## Management")
    tooltip = discord.ui.TextDisplay(
        "As a game host, you can declare the result of this game null and void. It will no longer affect ratings or statistics, be viewable by searching, or appear in Regle, Wille, or Logle."
    )
    annul_row = discord.ui.ActionRow()

    def __init__(self, admin: Admin, gist: str, filename: str, content: str, timecode: Timecode) -> None:
        super().__init__(accent_colour=discord.Colour(0x481052))
        self.bot = admin.bot
        self.admin = admin
        self.gist = gist
        self.filename = filename
        self.content = content
        self.timecode = timecode

    @needs_db
    async def add_section(self, conn: Connection, title: str, query: str, params: SqlParams) -> None:
        async with conn.execute(query, params) as cur:
            columns = [t[0] for t in cur.get_cursor().description]
            rows = await cur.fetchall()
        content = "\n".join("|".join(map(str, row)) for row in [columns, *rows])
        self.display.content += f"### {title}\n```{content}```\n"

    async def start(self) -> None:
        await self.add_section("Logs", "SELECT hash, filename, channel_id, message_id, attachment_id, filename_time, uploader, timecode, qualified FROM Gamelogs WHERE game = ?", (self.gist,))
        await self.add_section("Game", "SELECT from_log, first_log, message_count, analysis_version, victor, hunt_reached, generation FROM Games WHERE gist = ?", (self.gist,))
        await self.add_section("Appearances", "SELECT player, starting_role, ending_role, faction, account_name, game_name, won, saw_hunt FROM Appearances WHERE game = ?", (self.gist,))
        self.insert_item_before(File(discord.File(io.BytesIO(self.content.encode()), filename=self.filename)), self.display)

    @annul_row.button(label="Annul", emoji=config.annul_emoji, style=discord.ButtonStyle.danger)
    async def annul(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        async with self.bot.acquire() as conn:
            await conn.execute("DELETE FROM WilleGames WHERE gist = ?", (self.gist,))
            await conn.execute("DELETE FROM RegleGames WHERE gist = ?", (self.gist,))
            await conn.execute("DELETE FROM Appearances WHERE game = ?", (self.gist,))
            await conn.execute("UPDATE Gamelogs SET game = NULL, qualified = 0 WHERE game = ?", (self.gist,))
            await conn.execute("DELETE FROM Games WHERE gist = ?", (self.gist,))
        await self.admin.rerun_after(self.timecode)

        self.tooltip.content = "Game annulled. Effects on ratings will take some time to be reflected."
        button.disabled = True
        await self.view.message.edit(**self.edit_args())

    async def destroy(self) -> None:
        self.annul.disabled = True


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

    async def rerun_after(self, timecode: Timecode) -> None:
        async with self.bot.acquire(assert_new=True) as conn:
            await conn.execute("UPDATE Globals SET generation = generation + 1")
            await conn.execute("UPDATE Games SET generation = generation + 1 FROM Gamelogs WHERE first_log = hash AND timecode < ?", (timecode,))
        self.stats.run_games()

    @commands.command(name="is")
    @commands.is_owner()
    async def is_(self, ctx: Context, a: PlayerInfo, b: PlayerInfo) -> None:
        """Treat 2 players as being the same from now on in statistics."""
        async with self.bot.acquire() as conn:
            if a.id == b.id:
                await ctx.send("I know.")
                return

            conflicts = await conn.fetchall("SELECT game FROM Appearances AS A1 INNER JOIN Appearances AS A2 USING (game) WHERE A1.player = ? AND A2.player = ?", (a.id, b.id))
            if conflicts:
                urls = [f"- {u}" for conflict, in conflicts if (u := await (await self.logs.fetch_log_with_gist(conflict)).url())] if len(conflicts) <= 10 else []
                await ctx.send(f"Refusing to merge players who have appeared together in {len(conflicts)} games.\n{'\n'.join(urls)}")
                return

            timecode, = await conn.fetchone("SELECT MIN(timecode) FROM Appearances WHERE player = ?", (b.id,))

            await conn.execute("UPDATE OR IGNORE DiscordConnections SET player = ? WHERE player = ?", (a.id, b.id))
            await conn.execute("UPDATE Names SET player = ? WHERE player = ?", (a.id, b.id))
            await conn.execute("DELETE FROM Appearances WHERE player = ?", (b.id,))

        await ctx.send(":+1:")
        if timecode:
            await self.rerun_after(timecode)

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

    @commands.command(name="viewgame")
    @needs_db
    async def view_game(self, conn: Connection, ctx: Context, *, filename: str) -> None:
        """See the internal data of a game, giving you the option to annul it."""
        gist = await conn.fetchone("SELECT game, filename, clean_content, timecode FROM Gamelogs WHERE filename = ?", (filename,))
        if not gist:
            await ctx.send("I don't have a log by that name.")
            return
        if not gist[0]:
            await ctx.send("That log doesn't correspond to a game. It may be invalid or marked as exhibition, or it may have been annulled.")
            return

        await ctx.send_container_view(ViewGamePanel(self, *gist))


async def setup(bot: Lookout):
    await bot.add_cog(Admin(bot))
