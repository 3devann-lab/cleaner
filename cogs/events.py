"""Welcome messages, auto-role, anti-raid / account-age screening and AFK notices."""
import logging
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.security import Level, sec
from core.ui import clip, make_embed, mentions, reply

log = logging.getLogger(__name__)

RAID_HOLD_SECONDS = 120


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.joins = defaultdict(deque)  # guild_id -> deque[(ts, member_id)]
        self.raid_until = {}
        self.afk = defaultdict(dict)  # guild_id -> {user_id: (reason, since)}

    async def cog_load(self):
        for r in await self.bot.db.fetch("SELECT guild_id, user_id, reason, since FROM afk"):
            self.afk[r["guild_id"]][r["user_id"]] = (r["reason"], r["since"])

    # ------------------------------------------------------------- joining
    async def _punish(self, member: discord.Member, action: str, why: str) -> str:
        try:
            if action == "timeout":
                await member.timeout(timedelta(hours=1), reason=why)
                return "timed out for 1h"
            if action == "kick":
                await member.kick(reason=why)
                return "kicked"
        except discord.HTTPException:
            return "could not be actioned (check my permissions and role position)"
        return "flagged"

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild, sm = member.guild, self.bot.security
        s = await sm.settings(guild.id)
        punished = False

        if not member.bot:
            now = time.time()
            if s["antiraid_enabled"]:
                window = self.joins[guild.id]
                window.append((now, member.id))
                while window and now - window[0][0] > s["raid_window"]:
                    window.popleft()
                if len(window) >= s["raid_threshold"] and self.raid_until.get(guild.id, 0) < now:
                    self.raid_until[guild.id] = now + RAID_HOLD_SECONDS
                    await sm.audit(guild.id, None, "🚨 raid detected", None, f"{len(window)} joins within {s['raid_window']}s. Action: {s['raid_action']}.")
                    if s["raid_action"] != "alert":
                        for _, uid in list(window):
                            if uid != member.id and (other := guild.get_member(uid)):
                                await self._punish(other, s["raid_action"], "Anti-raid: join flood")
                if self.raid_until.get(guild.id, 0) >= now and s["raid_action"] != "alert":
                    result = await self._punish(member, s["raid_action"], "Anti-raid: join flood")
                    await sm.audit(guild.id, None, "anti-raid", f"{member} ({member.id})", result)
                    punished = True

            min_age = s["min_account_age_days"]
            if min_age and not punished:
                age_days = (discord.utils.utcnow() - member.created_at).days
                if age_days < min_age:
                    action = s["raid_action"]
                    result = await self._punish(member, action, f"Account younger than {min_age} day(s)") if action != "alert" else "flagged"
                    await sm.audit(guild.id, None, "new account", f"{member} ({member.id})", f"Account is {age_days} day(s) old (minimum {min_age}). Member {result}.")
                    punished = action == "kick"

        if punished and s["raid_action"] == "kick":
            return

        if s["autorole_id"]:
            role = guild.get_role(s["autorole_id"])
            if role and not role.managed and role < guild.me.top_role:
                try:
                    await member.add_roles(role, reason="Auto-role")
                except discord.HTTPException:
                    log.info("Could not assign auto-role in %s", guild.id)

        if s["welcome_channel_id"] and s["welcome_message"] and not member.bot:
            channel = guild.get_channel(s["welcome_channel_id"])
            if channel is not None:
                text = (
                    s["welcome_message"]
                    .replace("{user}", member.mention)
                    .replace("{name}", member.display_name)
                    .replace("{server}", guild.name)
                    .replace("{count}", str(guild.member_count))
                )
                try:
                    await channel.send(clip(text, 1900), allowed_mentions=mentions([member]))
                except discord.HTTPException:
                    log.info("Could not send welcome message in %s", guild.id)

    # ------------------------------------------------------------------ AFK
    @commands.Cog.listener()
    async def on_afk_changed(self, guild_id: int, user_id: int, state: bool):
        row = await self.bot.db.fetchrow("SELECT reason, since FROM afk WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
        if row:
            self.afk[guild_id][user_id] = (row["reason"], row["since"])
        else:
            self.afk[guild_id].pop(user_id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        table = self.afk.get(message.guild.id)
        if not table:
            return
        if message.author.id in table:
            # Ignore the very first message sent right after /afk (within 3 seconds)
            _, since = table[message.author.id]
            if (discord.utils.utcnow() - since).total_seconds() > 3:
                table.pop(message.author.id, None)
                await self.bot.db.execute("DELETE FROM afk WHERE guild_id=$1 AND user_id=$2", message.guild.id, message.author.id)
                try:
                    await message.channel.send(f"Welcome back {message.author.mention}, I removed your AFK.", delete_after=8, allowed_mentions=mentions([message.author]))
                except discord.HTTPException:
                    pass
        shown = 0
        for user in message.mentions:
            if shown >= 3:
                break
            if user.id in table and user.id != message.author.id:
                reason, since = table[user.id]
                shown += 1
                try:
                    await message.channel.send(f"**{discord.utils.escape_markdown(user.display_name)}** is AFK since {discord.utils.format_dt(since, 'R')}" + (f": {clip(reason, 100)}" if reason else ""), delete_after=10)
                except discord.HTTPException:
                    pass

    # --------------------------------------------------------------- /setup
    setup_group = app_commands.Group(name="setup", description="Welcome message and auto-role", guild_only=True)

    @setup_group.command(name="welcome", description="Set the welcome channel and message (leave both empty to disable)")
    @app_commands.describe(channel="Where to greet new members", message="Use {user} {name} {server} {count}")
    @sec(Level.ADMIN)
    async def setup_welcome(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, message: Optional[app_commands.Range[str, 1, 500]] = None):
        sm = self.bot.security
        if channel is not None and not channel.permissions_for(interaction.guild.me).send_messages:
            return await reply(interaction, content="I can't send messages in that channel.")
        await sm.ensure_settings_row(interaction.guild.id)
        await self.bot.db.execute(
            "UPDATE guild_settings SET welcome_channel_id=$2, welcome_message=$3 WHERE guild_id=$1",
            interaction.guild.id, channel.id if channel else None, (message or "Welcome {user} to **{server}**! You are member #{count}.") if channel else None,
        )
        sm.invalidate(interaction.guild.id)
        await sm.audit(interaction.guild.id, interaction.user.id, "setup:welcome", channel.mention if channel else "disabled")
        await reply(interaction, content=f"Welcome messages will be sent in {channel.mention}." if channel else "Welcome messages disabled.")

    @setup_group.command(name="autorole", description="Give new members a role automatically (leave empty to disable)")
    @app_commands.describe(role="Role to give")
    @sec(Level.ADMIN)
    async def setup_autorole(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        sm = self.bot.security
        if role is not None:
            if role.is_default() or role.managed:
                return await reply(interaction, content="That role can't be assigned.")
            if role >= interaction.guild.me.top_role:
                return await reply(interaction, content="My highest role must be above that role.")
            if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
                return await reply(interaction, content="You can only set roles below your own highest role.")
            dangerous = role.permissions.administrator or role.permissions.manage_guild or role.permissions.ban_members
            if dangerous:
                return await reply(interaction, content="For safety, auto-role can't be a role with Administrator, Manage Server or Ban Members.")
        await sm.ensure_settings_row(interaction.guild.id)
        await self.bot.db.execute("UPDATE guild_settings SET autorole_id=$2 WHERE guild_id=$1", interaction.guild.id, role.id if role else None)
        sm.invalidate(interaction.guild.id)
        await sm.audit(interaction.guild.id, interaction.user.id, "setup:autorole", role.mention if role else "disabled")
        await reply(interaction, content=f"New members will get {role.mention}." if role else "Auto-role disabled.")

    @setup_group.command(name="show", description="Show the current welcome and auto-role setup")
    @sec(Level.ADMIN)
    async def setup_show(self, interaction: discord.Interaction):
        s = await self.bot.security.settings(interaction.guild.id)
        embed = make_embed("Setup", kind="info")
        embed.add_field(name="Welcome channel", value=f"<#{s['welcome_channel_id']}>" if s["welcome_channel_id"] else "off")
        embed.add_field(name="Auto-role", value=f"<@&{s['autorole_id']}>" if s["autorole_id"] else "off")
        embed.add_field(name="Welcome message", value=clip(s["welcome_message"] or "-", 500), inline=False)
        await reply(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
