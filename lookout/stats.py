import asyncio
import contextvars
import datetime
import math
import logging
import textwrap
from typing import AsyncIterator, Sequence, Any

import discord
import gamelogs
from discord.ext import commands
from openskill.models import PlackettLuceRating

import config
from .bot import *
from .criteria import Criterion, RatingCriterion, DisplayablePlayer, Key
from .logs import gist_of, Timecode, Gamelogs
from .player_info import PlayerInfo, model
from .specifiers import IdentitySpecifier
from .views import ViewContainer, ContainerView, ConfirmationView
from .winrate import Winrate


log = logging.getLogger(__name__)


class Jump(discord.ui.Modal):
    def __init__(self, container: TopPaginator) -> None:
        super().__init__(title="Jump to page")
        self.container = container
        self.box.component.default = f"{container.page+1}"  # type: ignore

    box = discord.ui.Label(text="Destination", description="A page number or name of a player to jump to.", component=discord.ui.TextInput(max_length=32))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t: str = self.box.component.value  # type: ignore
        try:
            page = int(t) - 1
        except ValueError:
            for idx, (player, _) in enumerate(self.container.players):
                if any([name.casefold() == t.casefold() for name in await player.names()]):
                    break
            else:
                await interaction.response.send_message(f"I don't know a player called '{t}'.", ephemeral=True)
                return
            self.container.go_to_page(idx // self.container.per_page)
        else:
            if not self.container.has_page(page):
                await interaction.response.send_message(f"Page number {page+1} is out of bounds.", ephemeral=True)
                return
            self.container.go_to_page(page)
        await self.container.draw()
        await interaction.response.edit_message(view=self.container.view)


class TopPaginator[K: Key](ViewContainer):
    display = discord.ui.TextDisplay("")

    def __init__(self, stats: Stats, crit: Criterion[K]) -> None:
        super().__init__(accent_colour=discord.Colour(0xfce703))
        self.stats = stats
        self.crit: Criterion = crit
        self.per_page = 15

    async def start(self) -> None:
        self.players = sorted(await self.crit.decorate_players(self.stats.now()), key=lambda p: p[1], reverse=True)
        self.go_to_page(0)
        await self.draw()

    def key_desc(self) -> str:
        desc = self.crit.desc()
        return "" if desc is None else f"Sorting by {desc}."

    def has_page(self, num: int) -> bool:
        return 0 <= num*self.per_page < len(self.players)

    async def _render_player(self, player: DisplayablePlayer, key: K, obscure: bool) -> str:
        user = await player.user()
        names = await player.names()
        return f"{f'{user.mention}' if user else ('\u200b'*obscure).join(names[0])} - {self.crit.show_key(key)}"

    async def draw(self, *, obscure: bool = False) -> None:
        start = self.page*self.per_page
        lb = "\n".join([f"{start+1}. {await self._render_player(player, key, obscure)}" for player, key in self.players[start:start+self.per_page]])
        self.display.content = f"# Leaderboard\n{self.key_desc()}\n{lb}\n-# Page {self.page+1} of {math.ceil(len(self.players) / self.per_page):,}"

    def go_to_page(self, num: int) -> None:
        self.page = num
        self.previous.disabled = not self.has_page(num - 1)
        self.next.disabled = not self.has_page(num + 1)

    ar = discord.ui.ActionRow()

    @ar.button(label="Prev", emoji="⬅️")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page - 1)
        await self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Next", emoji="➡️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page + 1)
        await self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Jump", emoji="↪️")
    async def jump(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(Jump(self))

    async def destroy(self) -> None:
        await self.draw(obscure=True)
        self.remove_item(self.ar)


class Stats(commands.Cog):
    """Player statistics."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot
        self.catchup = asyncio.Lock()

    async def cog_load(self) -> None:
        self.catchup_task = asyncio.create_task(self.run_games())

    async def cog_unload(self) -> None:
        self.catchup_task.cancel()

    @needs_db
    async def update_game(self, conn: Connection, gist: str, from_log: str) -> None:
        content, = await conn.fetchone("SELECT clean_content FROM Gamelogs WHERE hash = ?", (from_log,))
        try:
            game, message_count = await asyncio.to_thread(gamelogs.parse, content, gamelogs.ResultAnalyzer(pandora=True) & gamelogs.MessageCountAnalyzer(), clean_tags=False)
        except gamelogs.BadLogError:
            log.exception("failed to update game from log %s", from_log)
        else:
            assert gist_of(game) == gist
            await conn.execute("UPDATE Games SET analysis = ?, message_count = ?, analysis_version = ? WHERE gist = ?", (game, message_count, gamelogs.version, gist))
            log.info("updated game from log %s to version %d", from_log, gamelogs.version)

    async def update_games(self, games: list[tuple[str, str]]) -> None:
        for gist, from_log in games:
            await self.update_game(gist, from_log)

    async def games(self, injection: str = "", params: Sequence[object] | dict[str, Any] = ()) -> list[gamelogs.GameResult]:
        async with self.bot.acquire() as conn:
            games = await conn.fetchall(f"SELECT gist, analysis, analysis_version, from_log FROM Games {injection}", params)
        r = []
        to_update = []
        for gist, game, version, from_log in games:
            if version < gamelogs.version:
                to_update.append((gist, from_log))
            r.append(game)
        if to_update:
            asyncio.create_task(self.update_games(to_update), context=contextvars.Context())
        return r

    @needs_db
    async def run_game(self, conn: Connection, game: gamelogs.GameResult):
        log = await self.bot.require_cog(Gamelogs).fetch_log(game)
        gist = gist_of(game)

        await conn.execute("DELETE FROM Appearances WHERE game = ?", (gist,))

        teams: list[list[tuple[gamelogs.Player, PlayerInfo]]] = [[], []]
        ratings: list[list[PlackettLuceRating]] = [[], []]
        for player in game.players:
            info: PlayerInfo = await self.fetch_player_by_name(player.account_name)  # type: ignore
            i = player.ending_ident.faction == gamelogs.coven
            teams[i].append((player, info))
            rating = await info.rating(log.first_upload, this_gen=True)
            ratings[i].append(rating.rating if rating else model.rating())

        # pad coven
        avg = sum([r.mu for r in ratings[1]]) / len(ratings[1])
        ratings[1].extend([model.rating(mu=avg, sigma=avg/3) for _ in range(len(ratings[0]) - len(ratings[1]))])

        new_ratings = model.rate(ratings, ranks=[1, 2] if game.victor == gamelogs.town else [2, 1])
        for team, team_ratings in zip(teams, new_ratings):
            for (player, info), new_rating in zip(team, team_ratings):
                await conn.execute("""
                    INSERT INTO Appearances (player, starting_role, ending_role, faction, game, account_name, game_name, won, saw_hunt, mu_after, sigma_after, timecode)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    info.id, player.starting_ident.role, player.ending_ident.role, player.ending_ident.faction, gist,
                    player.account_name, player.game_name, player.won, game.saw_hunt(player),
                    new_rating.mu, new_rating.sigma, log.first_upload,
                ))

        await conn.execute("UPDATE Games SET generation = Globals.generation FROM Globals WHERE gist = ?", (gist,))

    async def run_games(self):
        try:
            async with self.catchup:
                log.info("resolving appearances")
                c = 0
                for game in await self.games("INNER JOIN Gamelogs ON hash = first_log, Globals WHERE Games.generation < Globals.generation ORDER BY timecode"):
                    await self.run_game(game)
                    c += 1
                log.info("resolved appearances from %d games", c)
        except Exception:
            log.exception("error running games")

    @commands.Cog.listener()
    async def on_saw_games(self):
        await self.run_games()

    def prev_update(self) -> datetime.datetime:
        return config.prev_update(datetime.datetime.now(datetime.timezone.utc))

    def next_update(self) -> datetime.datetime | None:
        return config.next_update(datetime.datetime.now(datetime.timezone.utc))

    def now(self) -> Timecode:
        return Timecode.from_datetime(self.prev_update())

    def fetch_player(self, player: int) -> PlayerInfo:
        return PlayerInfo(player, self.bot)

    @needs_db
    async def fetch_player_by_name(self, conn: Connection, name: str) -> PlayerInfo | None:
        r = await conn.fetchone("SELECT player FROM Names WHERE name = ?", (name,))
        return PlayerInfo(r[0], self.bot) if r else None

    @needs_db
    async def fetch_players(self, conn: Connection) -> list[PlayerInfo]:
        return [PlayerInfo(player, self.bot) for player, in await conn.fetchall(f"SELECT DISTINCT player FROM Names")]

    @needs_db
    async def fetch_discord_players(self, conn: Connection) -> list[PlayerInfo]:
        return [PlayerInfo(player, self.bot) for player, in await conn.fetchall(f"SELECT player FROM DiscordConnections ORDER BY player")]

    @commands.command()
    @needs_db
    async def info(self, conn: Connection, ctx: Context) -> None:
        """General information and statistics on stored games."""
        town_maj, coven_maj, town_hunt, coven_hunt = await conn.fetchone("""SELECT
            SUM(victor = 'town' AND NOT hunt_reached), SUM(victor = 'coven' AND NOT hunt_reached),
            SUM(victor = 'town' AND hunt_reached), SUM(victor = 'coven' AND hunt_reached)
        FROM Games""")
        total = town_maj + town_hunt + coven_maj + coven_hunt

        next_on = f", next on {discord.utils.format_dt(d)}" if (d := self.next_update()) else ""
        embed = discord.Embed(
            title="Lookout",
            description=textwrap.dedent(f"""
                Dutifully tracking {total:,} games,
                {town_maj + town_hunt:,} Town victories,
                {coven_maj + coven_hunt:,} Coven victories ({coven_maj:,} by majority, {coven_hunt:,} by countdown),
                {town_hunt + coven_hunt:,} hunts ({town_hunt:,} won by Town, {coven_hunt:,} won by Coven),
                and {total*15:,} fun times had.

                Last rating update on {discord.utils.format_dt(self.prev_update(), "D")}{next_on}
                Made with love by {config.me_emoji} <3
            """),
            colour=discord.Colour.dark_green(),
        )

        await ctx.send(embed=embed)

    @needs_db
    async def winrate_in(self, conn: Connection, player: PlayerInfo, spec: IdentitySpecifier = IdentitySpecifier()) -> Winrate:
        c, p = spec.to_sql()
        s, n = await conn.fetchone(
            f"SELECT COALESCE(SUM(won), 0), COUNT(*) FROM Appearances WHERE player = :player AND {c}",
            {"player": player.id, **p},
        )
        return Winrate(s, n)

    @commands.command()
    @needs_db
    async def player(self, conn: Connection, ctx: Context, *, player: PlayerInfo) -> None:
        """Show information about a player."""
        embed = discord.Embed()
        names = await player.names()
        user = await player.user()

        r = None
        for name in names:
            r = await conn.fetchone("SELECT thread_id FROM Blacklists WHERE account_name = ?", (name,))
            if r:
                break

        name_string = ", ".join(names)
        title = f"{user.mention} ({name_string})" if user else name_string

        overall = await self.winrate_in(player)
        game_count = f"{overall.n:,} games" if overall.n != 1 else "1 game"
        embed.set_footer(text=f"Seen in {game_count}")

        hidden = await player.hidden()
        if hidden == "user":
            embed.description = f"### {title}\nThis player has chosen to hide their profile."
        elif hidden == "cheated":
            embed.description = f"### {title}\nThis player's profile is hidden because they were found to have played illegitimately."
        else:
            rating = await player.rating(self.now())
            rated = f"Rated {rating.ordinal():.0f} (#{await rating.rank():,})" if rating else "Not rated"
            embed.description = f"### {title}\n{rated}"
            embed.add_field(name="Winrates", value=textwrap.dedent(f"""
                - Overall {overall}
                - Town {await self.winrate_in(player, IdentitySpecifier().with_faction(gamelogs.town))}
                - Purple {await self.winrate_in(player, IdentitySpecifier().with_faction(gamelogs.coven))}
                  - Coven {await self.winrate_in(player, IdentitySpecifier().where(lambda role: role.default_faction != gamelogs.town))}
                  - TT {await self.winrate_in(player, IdentitySpecifier().where(lambda role: role.default_faction == gamelogs.town).with_faction(gamelogs.coven))}
            """))
            embed.add_field(name="Winrates in hunt", value=textwrap.dedent(f"""
                - Town {await self.winrate_in(player, IdentitySpecifier(hunt=True).with_faction(gamelogs.town))}
                - TT {await self.winrate_in(player, IdentitySpecifier(hunt=True).with_faction(gamelogs.coven))}
            """))

        if r:
            embed.add_field(name="Player blacklisted", value=f"<#{r[0]}>", inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=["lb", "leaderboard", "players"])
    @needs_db
    async def top(self, conn: Connection, ctx: Context, *, criterion: Criterion = commands.param(default=lambda ctx: RatingCriterion(ctx.bot))) -> None:
        """Rank all players by a specified criterion.

        By default, `top` displays players sorted by their rating. Specifying an alignment or role, such as `town`, will sort by winrate instead.
        Prefix with the `played` keyword to count games played instead of winrate.
        """
        view = ContainerView(ctx.author, TopPaginator(self, criterion))
        await view.container.start()
        view.message = await ctx.send(view=view)

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
            logs = self.bot.require_cog(Gamelogs)
            urls = [f"- {u}" for conflict, in conflicts if (u := await (await logs.fetch_log_with_gist(conflict)).url())] if len(conflicts) <= 10 else []
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

        await self.run_games()

    @commands.command()
    @needs_db
    async def hide(self, conn: Connection, ctx: Context) -> None:
        """Hide the information the bot tracks about your gameplay."""
        r = await conn.fetchone("INSERT OR REPLACE INTO Hidden (player) SELECT player FROM DiscordConnections WHERE discord_id = ? RETURNING player", (ctx.author.id,))
        if not r:
            await ctx.send("Sorry, I don't know who you are.")
            return
        await ctx.send(":+1:")

    @commands.command(aliases=["unhide"])
    @needs_db
    async def show(self, conn: Connection, ctx: Context) -> None:
        """Revert the effect of `hide`."""
        r = await conn.fetchone("SELECT player FROM DiscordConnections WHERE discord_id = ?", (ctx.author.id,))
        if not r:
            await ctx.send("Sorry, I don't know who you are.")
            return
        await conn.execute("DELETE FROM Hidden WHERE player = ?", (r[0],))
        await ctx.send(":+1:")

    @commands.command()
    @commands.is_owner()
    @needs_db
    async def cheated(self, conn: Connection, ctx: Context, player: PlayerInfo) -> None:
        """Mark a player as having cheated."""
        await conn.execute("INSERT OR REPLACE INTO Hidden (player, why) VALUES (?, 'cheated')", (player.id,))
        await ctx.send(":+1:")

    @commands.command()
    @commands.is_owner()
    @needs_db
    async def uncheated(self, conn: Connection, ctx: Context, player: PlayerInfo) -> None:
        """Revert the effect of `cheated`."""
        await conn.execute("DELETE FROM Hidden WHERE player = ?", (player.id,))
        await ctx.send(":+1:")

    @commands.command()
    @commands.check_any(commands.has_role("Game host"), commands.is_owner())
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
    @commands.check_any(commands.has_role("Game host"), commands.is_owner())
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
    await bot.add_cog(Stats(bot))
