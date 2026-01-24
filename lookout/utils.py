from __future__ import annotations

import discord


class ContainerView[T: discord.ui.Container](discord.ui.LayoutView):
    message: discord.Message

    def __init__(self, owner: discord.User | discord.Member, container: T) -> None:
        super().__init__()
        self.owner = owner
        self.container = container
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message("You can't control this element.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        await self.container.destroy()  # type: ignore
        await self.message.edit(view=self)
