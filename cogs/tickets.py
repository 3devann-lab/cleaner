"""Private support tickets with a persistent 'Open ticket' panel."""
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.security import Level, sec
from core.ui import make_embed, reply

log = logging.getLogger(__name__)


class OpenTicket(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:open"):
    def __init__(self):
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.primary, label="🎫 Open a ticket", custom_id="ticket:open"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction.client.security.check_component(interaction)

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Tickets").open_ticket(interaction)


class CloseTicket(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:close"):
    def __init__(self):
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.danger, label="Close ticket", custom_id="ticket:close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction.client.security.check_component(interaction)

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Tickets").close_ticket(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_dynamic_items(OpenTicket, CloseTicket)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(OpenTicket, CloseTicket)

    ticket = app_commands.Group(name="ticket", description="Support tickets", guild_only=True)

    @ticket.command(name="setup", description="Choose where tickets are created and who can see them")
    @app_commands.describe(category="Category for new ticket channels", staff_role="Role that can see and manage tickets")
    @sec(Level.ADMIN)
    async def setup_tickets(self, interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role):
        sm = self.bot.security
        me = interaction.guild.me
        perms = category.permissions_for(me)
        if not (perms.manage_channels and perms.view_channel):
            return await reply(interaction, content="I need View Channel and Manage Channels in that category.")
        await sm.ensure_settings_row(interaction.guild.id)
        await self.bot.db.execute("UPDATE guild_settings SET ticket_category_id=$2, ticket_staff_role_id=$3 WHERE guild_id=$1", interaction.guild.id, category.id, staff_role.id)
        sm.invalidate(interaction.guild.id)
        await sm.audit(interaction.guild.id, interaction.user.id, "ticket:setup", category.name, f"Staff role: {staff_role.id}")
        await reply(interaction, content="Ticket system configured. Use `/ticket panel` to post the button.")

    @ticket.command(name="panel", description="Post the 'Open a ticket' button in this channel")
    @sec(Level.ADMIN)
    async def panel(self, interaction: discord.Interaction):
        s = await self.bot.security.settings(interaction.guild.id)
        if not s["ticket_category_id"] or not s["ticket_staff_role_id"]:
            return await reply(interaction, content="Run `/ticket setup` first.")
        view = discord.ui.View(timeout=None)
        view.add_item(OpenTicket())
        await interaction.response.send_message(embed=make_embed("Need help?", "Press the button below to open a private ticket with the staff team.", "info"), view=view)

    async def open_ticket(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        s = await self.bot.security.settings(guild.id)
        category = guild.get_channel(s["ticket_category_id"]) if s["ticket_category_id"] else None
        staff = guild.get_role(s["ticket_staff_role_id"]) if s["ticket_staff_role_id"] else None
        if category is None or staff is None:
            return await interaction.response.send_message("Tickets aren't configured correctly. Tell an admin.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        existing = await self.bot.db.fetchrow("SELECT channel_id FROM tickets WHERE guild_id=$1 AND user_id=$2 AND status='open'", guild.id, user.id)
        if existing and existing["channel_id"] and guild.get_channel(existing["channel_id"]):
            return await interaction.followup.send(f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
        if existing:
            await self.bot.db.execute("UPDATE tickets SET status='closed' WHERE guild_id=$1 AND user_id=$2 AND status='open'", guild.id, user.id)

        ticket_id = await self.bot.db.fetchval("INSERT INTO tickets (guild_id, user_id) VALUES ($1,$2) RETURNING id", guild.id, user.id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
            staff: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, embed_links=True),
        }
        try:
            channel = await guild.create_text_channel(f"ticket-{ticket_id:04d}", category=category, overwrites=overwrites, reason=f"Ticket opened by {user} ({user.id})")
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM tickets WHERE id=$1", ticket_id)
            return await interaction.followup.send("I couldn't create the ticket channel. Tell an admin to check my permissions.", ephemeral=True)
        await self.bot.db.execute("UPDATE tickets SET channel_id=$2 WHERE id=$1", ticket_id, channel.id)

        view = discord.ui.View(timeout=None)
        view.add_item(CloseTicket())
        await channel.send(
            content=f"{user.mention} {staff.mention}",
            embed=make_embed(f"Ticket #{ticket_id:04d}", "Describe your issue and staff will be with you soon.", "info"),
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff]),
        )
        await self.bot.security.audit(guild.id, user.id, "ticket:open", f"<#{channel.id}>")
        await interaction.followup.send(f"Your ticket: {channel.mention}", ephemeral=True)

    async def close_ticket(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchrow("SELECT * FROM tickets WHERE channel_id=$1 AND status='open'", interaction.channel_id)
        if row is None:
            return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
        s = await self.bot.security.settings(interaction.guild.id)
        is_staff = any(r.id == s["ticket_staff_role_id"] for r in interaction.user.roles) or await self.bot.security.member_level(interaction.user) >= Level.MOD
        if interaction.user.id != row["user_id"] and not is_staff:
            return await interaction.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await self.bot.db.execute("UPDATE tickets SET status='closed' WHERE id=$1", row["id"])
        await interaction.response.send_message("Closing this ticket in 5 seconds...")
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "ticket:close", f"#{row['id']:04d}")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException:
            pass

    async def _ticket_row(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchrow("SELECT * FROM tickets WHERE channel_id=$1 AND status='open'", interaction.channel_id)
        if row is None:
            await reply(interaction, content="Use this inside an open ticket channel.")
        return row

    @ticket.command(name="add", description="Add a member to this ticket")
    @app_commands.describe(member="Member to add")
    @sec(Level.MOD)
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        if await self._ticket_row(interaction) is None:
            return
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, reason=f"Added by {interaction.user} ({interaction.user.id})")
        await reply(interaction, content=f"Added {member.mention}.")

    @ticket.command(name="remove", description="Remove a member from this ticket")
    @app_commands.describe(member="Member to remove")
    @sec(Level.MOD)
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        row = await self._ticket_row(interaction)
        if row is None:
            return
        if member.id == row["user_id"]:
            return await reply(interaction, content="You can't remove the ticket owner. Close the ticket instead.")
        await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed by {interaction.user} ({interaction.user.id})")
        await reply(interaction, content=f"Removed {member.mention}.")

    @ticket.command(name="close", description="Close this ticket")
    @sec(Level.MOD)
    async def close(self, interaction: discord.Interaction):
        if await self._ticket_row(interaction) is None:
            return
        await self.close_ticket(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
