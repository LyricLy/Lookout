import asyncio
import contextvars
import datetime
import math
import logging
import textwrap

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
from .views import ViewContainer
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

    def __init__(self, crit: Criterion[K], at: Timecode) -> None:
        super().__init__(accent_colour=discord.Colour(0xfce703))
        self.at = at
        self.crit: Criterion = crit
        self.per_page = 15

    async def start(self) -> None:
        self.players = sorted(await self.crit.decorate_players(self.at), key=lambda p: p[1], reverse=True)
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
        self.run_games()

    async def update_games(self, games: list[tuple[str, str, gamelogs.Faction]]) -> None:
        victors_changed = False

        for gist, from_log, old_victor in games:
            async with self.bot.acquire() as conn:
                content, = await conn.fetchone("SELECT clean_content FROM Gamelogs WHERE hash = ?", (from_log,))

            try:
                game, message_count = await asyncio.to_thread(gamelogs.parse, content, gamelogs.ResultAnalyzer(pandora=True) & gamelogs.MessageCountAnalyzer(), clean_tags=False)
            except gamelogs.BadLogError:
                log.exception("failed to update game from log %s", from_log)
                return

            assert gist_of(game) == gist
            async with self.bot.acquire() as conn:
                await conn.execute("UPDATE Games SET analysis = ?, message_count = ?, analysis_version = ?, victor = ? WHERE gist = ?", (game, message_count, gamelogs.version, game.victor, gist))

            if game.victor != old_victor:
                victors_changed = True

            log.info("updated game from log %s to version %d", from_log, gamelogs.version)

        if victors_changed:
            async with self.bot.acquire() as conn:
                await conn.execute("UPDATE Globals SET generation = generation + 1")
            self.run_games()

    async def games(self, injection: str = "", params: SqlParams = (), *, current: bool = False) -> list[gamelogs.GameResult]:
        async with self.bot.acquire() as conn:
            games = await conn.fetchall(f"SELECT gist, analysis, analysis_version, from_log, victor FROM Games {injection}", params)

        r = []
        to_update = []
        for gist, game, version, from_log, victor in games:
            if version < gamelogs.version:
                to_update.append((gist, from_log, victor))
            r.append(game)

        if to_update:
            update = self.update_games(to_update)
            if current:
                await update
                return await self.games(injection, params, current=True)
            asyncio.create_task(update, context=contextvars.Context())

        return r

    @needs_db
    async def run_game(self, conn: Connection, game: gamelogs.GameResult):
        gist = gist_of(game)
        await conn.execute("DELETE FROM Appearances WHERE game = ?", (gist,))
        log = await self.bot.require_cog(Gamelogs).fetch_log(game)

        teams: list[list[tuple[gamelogs.Player, PlayerInfo]]] = [[], []]
        ratings: list[list[PlackettLuceRating]] = [[], []]
        for player in game.players:
            info: PlayerInfo = await PlayerInfo.by_name(conn, player.account_name, self.bot)  # type: ignore
            i = player.ending_ident.faction == gamelogs.coven
            teams[i].append((player, info))
            rating = await info.rating(log.first_upload, this_gen=True)
            ratings[i].append(rating.rating if rating else model.rating())

        # pad coven
        avg = sum([r.mu for r in ratings[1]]) / len(ratings[1])
        ratings[1].extend([model.rating(mu=avg, sigma=avg/3) for _ in range(len(ratings[0]) - len(ratings[1]))])

        new_ratings = model.rate(ratings, ranks=[1, 2] if game.victor == gamelogs.town else [2, 1] if game.victor == gamelogs.coven else [1, 1])
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

    async def _run_games(self):
        try:
            async with self.catchup:
                log.info("resolving appearances")
                c = 0
                for game in await self.games("INNER JOIN Gamelogs ON hash = first_log, Globals WHERE Games.generation < Globals.generation ORDER BY timecode", current=True):
                    await self.run_game(game)
                    c += 1
                log.info("resolved appearances from %d games", c)
        except Exception:
            log.exception("error running games")

    def run_games(self):
        asyncio.create_task(self._run_games(), context=contextvars.Context())

    @commands.Cog.listener()
    async def on_saw_games(self):
        self.run_games()

    def prev_update(self) -> datetime.datetime:
        return config.prev_update(datetime.datetime.now(datetime.timezone.utc))

    def next_update(self) -> datetime.datetime | None:
        return config.next_update(datetime.datetime.now(datetime.timezone.utc))

    def now(self) -> Timecode:
        return Timecode.from_datetime(self.prev_update())

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
        await ctx.send_container_view(TopPaginator(criterion, self.now()))

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


async def setup(bot: Lookout):
    await bot.add_cog(Stats(bot))
