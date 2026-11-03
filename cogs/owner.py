"""/owner - commands only the people in OWNER_IDS can run."""
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.security import Level, sec
from core.ui import clip, confirm, make_embed, reply

log = logging.getLogger(__name__)


class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    owner = app_commands.Group(name="owner", description="Bot-owner tools", guild_only=True)

    @owner.command(name="gblacklist", description="Block a user from the bot everywhere")
    @app_commands.describe(user="User to block", reason="Why")
    @sec(Level.BOT_OWNER)
    async def gblacklist(self, interaction: discord.Interaction, user: discord.User, reason: Optional[app_commands.Range[str, 1, 200]] = None):
        if user.id in config.OWNER_IDS or user.bot:
            return await reply(interaction, content="You can't blacklist an owner or a bot.")
        await self.bot.db.execute(
            "INSERT INTO blacklist (guild_id, user_id, reason, added_by) VALUES (0,$1,$2,$3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET reason=EXCLUDED.reason, added_by=EXCLUDED.added_by",
            user.id, reason, interaction.user.id,
        )
        await self.bot.security.load_global()
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "global-blacklist:add", f"<@{user.id}>", reason)
        await reply(interaction, content=f"{user} is now blocked everywhere.")

    @owner.command(name="gunblacklist", description="Remove a global block")
    @app_commands.describe(user="User to unblock")
    @sec(Level.BOT_OWNER)
    async def gunblacklist(self, interaction: discord.Interaction, user: discord.User):
        await self.bot.db.execute("DELETE FROM blacklist WHERE guild_id=0 AND user_id=$1", user.id)
        await self.bot.security.load_global()
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "global-blacklist:remove", f"<@{user.id}>")
        await reply(interaction, content=f"{user} can use the bot again.")

    @owner.command(name="gblacklisted", description="List globally blocked users")
    @sec(Level.BOT_OWNER)
    async def gblacklisted(self, interaction: discord.Interaction):
        bl = self.bot.security.global_blacklist
        if not bl:
            return await reply(interaction, content="No globally blocked users.")
        await reply(interaction, embed=make_embed("Global blacklist", "\n".join(f"<@{uid}> - {clip(r or 'No reason', 80)}" for uid, r in list(bl.items())[:40]), "warn"))

    @owner.command(name="sync", description="Re-sync slash commands with Discord")
    @sec(Level.BOT_OWNER)
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        synced = await self.bot.tree.sync()
        await reply(interaction, content=f"Synced {len(synced)} global command(s). Discord can take a few minutes to show changes.")

    @owner.command(name="guilds", description="List the servers the bot is in")
    @sec(Level.BOT_OWNER)
    async def guilds(self, interaction: discord.Interaction):
        lines = [f"`{g.id}` {clip(discord.utils.escape_markdown(g.name), 40)} - {g.member_count} members" for g in sorted(self.bot.guilds, key=lambda g: -(g.member_count or 0))[:40]]
        await reply(interaction, embed=make_embed(f"{len(self.bot.guilds)} server(s)", "\n".join(lines) or "None", "info"))

    @owner.command(name="leave", description="Make the bot leave a server")
    @app_commands.describe(guild_id="Server ID")
    @sec(Level.BOT_OWNER)
    async def leave(self, interaction: discord.Interaction, guild_id: str):
        if not guild_id.isdigit() or (guild := self.bot.get_guild(int(guild_id))) is None:
            return await reply(interaction, content="I'm not in a server with that ID.")
        if not await confirm(interaction, "Leave server?", f"Leave **{discord.utils.escape_markdown(guild.name)}** (`{guild.id}`)?"):
            return
        await guild.leave()
        await interaction.edit_original_response(embed=make_embed("Left the server.", kind="ok"), view=None)

    @owner.command(name="reload", description="Reload a cog without restarting")
    @app_commands.describe(cog="Cog module name, e.g. utility")
    @sec(Level.BOT_OWNER)
    async def reload(self, interaction: discord.Interaction, cog: str):
        name = f"cogs.{cog.strip().lower()}"
        if name not in self.bot.extensions:
            return await reply(interaction, content="Loaded cogs: " + ", ".join(e.split(".")[-1] for e in self.bot.extensions))
        await self.bot.reload_extension(name)
        await reply(interaction, content=f"Reloaded `{name}`. Run `/owner sync` if commands changed.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
