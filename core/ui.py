"""Shared Discord UI helpers: embeds, safe replies, confirmation buttons."""
import discord

COLOR = {
    "ok": 0x57F287,
    "err": 0xED4245,
    "info": 0x5865F2,
    "warn": 0xFEE75C,
}

NO_MENTIONS = discord.AllowedMentions.none()


def mentions(users=()):
    """AllowedMentions that can ping ONLY the given users (never @everyone/roles)."""
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        replied_user=False,
        users=[discord.Object(id=u if isinstance(u, int) else u.id) for u in users],
    )


def make_embed(title=None, description=None, kind="info", **kwargs) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR[kind], **kwargs)


def clip(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def reply(interaction: discord.Interaction, *, content=None, embed=None, view=None, ephemeral=True):
    """Respond to an interaction whether or not it was already acknowledged."""
    if interaction.response.is_done():
        await interaction.edit_original_response(content=content, embed=embed, view=view)
        return
    kwargs = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    await interaction.response.send_message(**kwargs)


class ConfirmView(discord.ui.View):
    """Confirm / Cancel buttons that only the person who ran the command can press."""

    def __init__(self, user_id: int, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.value = None  # True = confirmed, False = cancelled, None = timed out

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


async def confirm(interaction: discord.Interaction, title: str, description: str) -> bool:
    """Show a danger confirmation. Returns True only if the invoker pressed Confirm."""
    view = ConfirmView(interaction.user.id)
    await reply(interaction, embed=make_embed(title, description, "warn"), view=view)
    await view.wait()
    if view.value:
        return True
    text = "Cancelled." if view.value is False else "Timed out - nothing was changed."
    await interaction.edit_original_response(embed=make_embed(text, kind="info"), view=None)
    return False
