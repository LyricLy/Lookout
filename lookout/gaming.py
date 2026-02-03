from __future__ import annotations

import io

import discord
import gamelogs
from discord.ext import commands

import config
from .bot import Lookout
from .logs import gist_of
from .utils import ContainerView


class ReglePanel(discord.ui.Container):
    display = discord.ui.Section("", accessory=discord.ui.Thumbnail(f"{config.base_url}/static/who_wins.png"))
    sep = discord.ui.Separator(spacing=discord.SeparatorSpacing.large)

    def __init__(self, bot: Lookout, game: gamelogs.GameResult) -> None:
        super().__init__()
        self.bot = bot
        self.game = game
        self.draw_players("Who won?")

    def draw_players(self, header: str, *, obscure: bool = False) -> None:
        players = sorted(self.game.players, key=lambda p: (
            (i := p.starting_ident).faction == gamelogs.coven,
            i.role.default_faction == gamelogs.coven,
            gamelogs.bucket_of[i.role] == "Coven Deception",
            gamelogs.bucket_of[i.role],
            i.role.name,
            p.account_name,
        ))
        lines = []
        for player in players:
            if player.starting_ident.is_wrong_faction():
                emoji = config.tt_emoji
                role = f"{player.starting_ident.role} (TT)"
            else:
                emoji = config.bucket_emoji[gamelogs.bucket_of[player.starting_ident.role]]
                role = player.starting_ident
            lines.append(f"{emoji} {('\u200b'*obscure).join(player.account_name)} - {role}")
        self.display.children[0].content = f"# {header}\n{'\n'.join(lines)}"  # type: ignore

    ar = discord.ui.ActionRow()

    async def finish(self, guess: gamelogs.Faction, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.bot.db.execute("SELECT filename, clean_content, message_id FROM Gamelogs INNER JOIN Games ON hash = from_log WHERE gist = ?", (gist_of(self.game),)) as cur:
            filename, content, message_id = await cur.fetchone()  # type: ignore
        file = discord.File(io.BytesIO(content.encode()), filename=filename)

        self.accent_colour = discord.Colour(0x06e00c if self.game.victor == gamelogs.town else 0xb545ff)
        thumbnail = "town_wins.png" if self.game.victor == gamelogs.town else "coven_wins.png"
        self.display.accessory.media = f"{config.base_url}/static/{thumbnail}"  # type: ignore

        correct = self.game.victor == guess
        button.style = discord.ButtonStyle.green if correct else discord.ButtonStyle.red
        header = "Correct!" if correct else "Aw..."
        self.end(f"{header}\nUploaded {discord.utils.format_dt(discord.utils.snowflake_time(message_id), 'D')}")

        # disgusting
        self._children.insert(self._children.index(self.sep), discord.ui.File(file))

        await self.bot.db.execute("INSERT INTO RegleGames (player_id, guessed, correct, gist) VALUES (?, ?, ?, ?)", (interaction.user.id, repr(guess), repr(self.game.victor), gist_of(self.game)))
        await self.bot.db.commit()

        self.view.stop()
        await interaction.response.edit_message(view=self.view, attachments=[file])

    @ar.button(label="Town", emoji=config.town_emoji, style=discord.ButtonStyle.grey)
    async def guess_town(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.finish(gamelogs.town, interaction, button)

    @ar.button(label="Coven", emoji=config.coven_emoji, style=discord.ButtonStyle.grey)
    async def guess_coven(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.finish(gamelogs.coven, interaction, button)

    def end(self, header: str) -> None:
        self.draw_players(header, obscure=True)
        self.guess_town.disabled = True
        self.guess_coven.disabled = True

    async def destroy(self) -> None:
        self.end("Timed out")


class Gaming(commands.Cog):
    """Little minigames you can play on Discord."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    @commands.command()
    async def regle(self, ctx: commands.Context) -> None:
        """Guess which faction won a game, given only the players and starting roles."""
        async with self.bot.db.execute("SELECT analysis FROM Games LIMIT 1 OFFSET ABS(RANDOM()) % (SELECT COUNT(*) FROM Games)") as cur:
            game, = await cur.fetchone()  # type: ignore
        view = ContainerView(ctx.author, ReglePanel(self.bot, game))
        view.message = await ctx.send(view=view)


async def setup(bot: Lookout):
    await bot.add_cog(Gaming(bot))
