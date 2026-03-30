import asyncio

import discord


class ViewContainer(discord.ui.Container["ContainerView"]):
    def send_args(self) -> dict:
        assert self.view
        return self.view.send_args()

    def edit_args(self) -> dict:
        assert self.view
        return self.view.edit_args()

    async def destroy(self) -> None:
        raise NotImplementedError
 

class File(discord.ui.File["ContainerView"]):
    __slots__ = ("file",)

    def __init__(self, media: discord.File, *args, **kwargs):
        self.file = media
        super().__init__(media, *args, **kwargs)

    @discord.ui.File.media.setter
    def media(self, value: discord.File):  # type: ignore
        self.file = value
        super().media = value

    def _update_view(self, view: ContainerView | None) -> None:
        if self.view:
            self.view._files.remove(self)
        if view:
            view._files.append(self)
        super()._update_view(view)


class ContainerView[T: ViewContainer](discord.ui.LayoutView):
    message: discord.Message
    _files: list[File]

    def __init__(self, owner: discord.abc.User, container: T) -> None:
        super().__init__()
        self.owner = owner
        self.container = container
        self._files = []
        self.add_item(container)

    def send_args(self) -> dict:
        return {"view": self, "files": self.files}

    def edit_args(self) -> dict:
        return {"view": self, "attachments": self.files}

    @property
    def files(self) -> list[discord.File]:
        return [x.file for x in self._files]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message("You can't control this element.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        await self.container.destroy()
        await self.message.edit(**self.edit_args())


class ConfirmationView(discord.ui.View):
    message: discord.Message

    def __init__(self, owner: discord.abc.User) -> None:
        super().__init__(timeout=30)
        self.owner = owner
        self.event = asyncio.Event()

    def wait(self):
        return self.event.wait()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message("You can't agree to this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.event.set()
        button.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        self.confirm.disabled = True
        await self.message.edit(view=self)
