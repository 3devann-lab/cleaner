"""/mod - moderation tools with role-hierarchy checks, confirmations and case history."""
import logging
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.helpers import format_duration, parse_duration
from core.security import Level, sec
from core.ui import clip, confirm, make_embed, reply

log = logging.getLogger(__name__)

Reason = app_commands.Range[str, 1, 300]


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tempban_loop.start()

    async def cog_unload(self):
        self.tempban_loop.cancel()

    mod = app_commands.Group(name="mod", description="Moderation tools", guild_only=True)

    # ------------------------------------------------------------- helpers
    async def _new_case(self, guild_id, user_id, moderator_id, action, reason, expires_at=None) -> int:
        number = await self.bot.db.fetchval(
            "INSERT INTO guild_settings (guild_id, case_counter) VALUES ($1, 1) "
            "ON CONFLICT (guild_id) DO UPDATE SET case_counter = guild_settings.case_counter + 1 "
            "RETURNING case_counter",
            guild_id,
        )
        await self.bot.db.execute(
            "INSERT INTO mod_cases (guild_id, case_number, user_id, moderator_id, action, reason, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            guild_id, number, user_id, moderator_id, action, reason, expires_at,
        )
        return number

    async def _finish(self, interaction, action, user, reason, expires_at=None, extra=""):
        """Record the case, write the audit log and answer the moderator."""
        guild = interaction.guild
        case = await self._new_case(guild.id, user.id, interaction.user.id, action, reason, expires_at)
        await self.bot.security.audit(
            guild.id, interaction.user.id, f"mod:{action}", f"{user} ({user.id})", f"Case #{case}: {reason}{extra}"
        )
        await reply(interaction, embed=make_embed(f"Case #{case} - {action}", f"**User:** {user.mention} (`{user.id}`)\n**Reason:** {reason}{extra}", "ok"))

    async def _dm(self, user, guild, verb: str, reason: str):
        try:
            await user.send(embed=make_embed(f"You were {verb} in {guild.name}", f"**Reason:** {reason}", "warn"))
        except discord.HTTPException:
            pass  # DMs closed - ignore

    async def _guard(self, interaction: discord.Interaction, member: discord.Member) -> Optional[str]:
        """Return an error message if the moderator may not act on this member."""
        guild, actor, sm = interaction.guild, interaction.user, self.bot.security
        if member.id == actor.id:
            return "You can't do that to yourself."
        if member.id == guild.owner_id:
            return "You can't moderate the server owner."
        if member.id == guild.me.id:
            return "I won't do that to myself."
        if actor.id != guild.owner_id:
            if actor.top_role <= member.top_role:
                return "Your highest role must be above the target's highest role."
            if await sm.member_level(member) >= await sm.member_level(actor):
                return "You can't moderate someone with equal or higher bot access than you."
        if guild.me.top_role <= member.top_role:
            return "My highest role must be above the target's highest role."
        return None

    async def _guard_user(self, interaction: discord.Interaction, user: discord.abc.User) -> Optional[str]:
        member = interaction.guild.get_member(user.id)
        if member is not None:
            return await self._guard(interaction, member)
        if user.id == interaction.user.id:
            return "You can't do that to yourself."
        if user.id == interaction.guild.me.id:
            return "I won't do that to myself."
        return None

    # --------------------------------------------------------------- kick
    @mod.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Reason (saved to the case log)")
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @sec(Level.MOD)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[Reason] = None):
        if err := await self._guard(interaction, member):
            return await reply(interaction, content=err)
        await interaction.response.defer(ephemeral=True)
        reason = reason or "No reason provided"
        await self._dm(member, interaction.guild, f"kicked from", reason)
        await member.kick(reason=clip(f"{interaction.user} ({interaction.user.id}): {reason}", 500))
        await self._finish(interaction, "kick", member, reason)

    # ---------------------------------------------------------------- ban
    @mod.command(name="ban", description="Ban a user (asks for confirmation)")
    @app_commands.describe(user="User to ban", reason="Reason (saved to the case log)", delete_days="Delete their messages from the last N days (0-7)")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @sec(Level.MOD)
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: Optional[Reason] = None, delete_days: app_commands.Range[int, 0, 7] = 0):
        if err := await self._guard_user(interaction, user):
            return await reply(interaction, content=err)
        if not await confirm(interaction, "Confirm ban", f"Ban **{user}** (`{user.id}`)?"):
            return
        reason = reason or "No reason provided"
        await self._dm(user, interaction.guild, "banned from", reason)
        await interaction.guild.ban(user, reason=clip(f"{interaction.user} ({interaction.user.id}): {reason}", 500), delete_message_seconds=delete_days * 86400)
        await self._finish(interaction, "ban", user, reason)

    @mod.command(name="tempban", description="Ban a user for a limited time")
    @app_commands.describe(user="User to ban", duration="e.g. 12h, 3d, 1w", reason="Reason (saved to the case log)")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @sec(Level.MOD)
    async def tempban(self, interaction: discord.Interaction, user: discord.User, duration: str, reason: Optional[Reason] = None):
        try:
            delta = parse_duration(duration, min_seconds=60, max_seconds=365 * 86400)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        if err := await self._guard_user(interaction, user):
            return await reply(interaction, content=err)
        if not await confirm(interaction, "Confirm temporary ban", f"Ban **{user}** (`{user.id}`) for {format_duration(delta.total_seconds())}?"):
            return
        reason = reason or "No reason provided"
        expires = discord.utils.utcnow() + delta
        await self._dm(user, interaction.guild, f"banned for {format_duration(delta.total_seconds())} from", reason)
        await interaction.guild.ban(user, reason=clip(f"Tempban by {interaction.user} ({interaction.user.id}): {reason}", 500), delete_message_seconds=0)
        await self._finish(interaction, "tempban", user, reason, expires, f"\n**Unban:** {discord.utils.format_dt(expires, 'R')}")

    @mod.command(name="unban", description="Remove a ban")
    @app_commands.describe(user="User to unban", reason="Reason")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @sec(Level.MOD)
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: Optional[Reason] = None):
        reason = reason or "No reason provided"
        try:
            await interaction.guild.unban(user, reason=clip(f"{interaction.user} ({interaction.user.id}): {reason}", 500))
        except discord.NotFound:
            return await reply(interaction, content="That user isn't banned.")
        await self.bot.db.execute(
            "UPDATE mod_cases SET active = FALSE WHERE guild_id=$1 AND user_id=$2 AND action='tempban' AND active",
            interaction.guild.id, user.id,
        )
        await self._finish(interaction, "unban", user, reason)

    # ------------------------------------------------------------ timeout
    @mod.command(name="timeout", description="Time a member out (they can't talk or react)")
    @app_commands.describe(member="Member to time out", duration="e.g. 10m, 2h, 1d (max 28d)", reason="Reason")
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @sec(Level.MOD)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: Optional[Reason] = None):
        try:
            delta = parse_duration(duration, min_seconds=60, max_seconds=28 * 86400)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        if err := await self._guard(interaction, member):
            return await reply(interaction, content=err)
        await interaction.response.defer(ephemeral=True)
        reason = reason or "No reason provided"
        await self._dm(member, interaction.guild, f"timed out for {format_duration(delta.total_seconds())} in", reason)
        await member.timeout(delta, reason=clip(f"{interaction.user} ({interaction.user.id}): {reason}", 500))
        await self._finish(interaction, "timeout", member, reason, discord.utils.utcnow() + delta, f"\n**Duration:** {format_duration(delta.total_seconds())}")

    @mod.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(member="Member to release", reason="Reason")
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @sec(Level.MOD)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[Reason] = None):
        if err := await self._guard(interaction, member):
            return await reply(interaction, content=err)
        await interaction.response.defer(ephemeral=True)
        reason = reason or "No reason provided"
        await member.timeout(None, reason=clip(f"{interaction.user} ({interaction.user.id}): {reason}", 500))
        await self._finish(interaction, "untimeout", member, reason)

    # -------------------------------------------------------------- warns
    @mod.command(name="warn", description="Warn a member (they get a DM and it is saved)")
    @app_commands.describe(member="Member to warn", reason="Why they are being warned")
    @sec(Level.MOD)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: Reason):
        if err := await self._guard(interaction, member):
            return await reply(interaction, content=err)
        await interaction.response.defer(ephemeral=True)
        await self._dm(member, interaction.guild, "warned in", reason)
        total = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM mod_cases WHERE guild_id=$1 AND user_id=$2 AND action='warn' AND active",
            interaction.guild.id, member.id,
        )
        await self._finish(interaction, "warn", member, reason, extra=f"\n**Active warnings:** {total + 1}")

    @mod.command(name="warnings", description="Show a member's active warnings")
    @app_commands.describe(user="User to look up")
    @sec(Level.MOD)
    async def warnings(self, interaction: discord.Interaction, user: discord.User):
        rows = await self.bot.db.fetch(
            "SELECT case_number, moderator_id, reason, created_at FROM mod_cases "
            "WHERE guild_id=$1 AND user_id=$2 AND action='warn' AND active ORDER BY case_number DESC LIMIT 15",
            interaction.guild.id, user.id,
        )
        if not rows:
            return await reply(interaction, content=f"{user} has no active warnings.")
        lines = [f"**#{r['case_number']}** {discord.utils.format_dt(r['created_at'], 'R')} by <@{r['moderator_id']}> - {clip(r['reason'] or '', 150)}" for r in rows]
        await reply(interaction, embed=make_embed(f"Warnings for {user}", "\n".join(lines), "warn"))

    @mod.command(name="delwarn", description="Remove a warning by its case number")
    @app_commands.describe(case_number="Case number of the warning")
    @sec(Level.MOD)
    async def delwarn(self, interaction: discord.Interaction, case_number: app_commands.Range[int, 1, 10_000_000]):
        row = await self.bot.db.fetchrow(
            "UPDATE mod_cases SET active = FALSE WHERE guild_id=$1 AND case_number=$2 AND action='warn' AND active RETURNING user_id",
            interaction.guild.id, case_number,
        )
        if row is None:
            return await reply(interaction, content="No active warning with that case number.")
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "mod:delwarn", f"Case #{case_number}", f"Warning for <@{row['user_id']}> removed")
        await reply(interaction, embed=make_embed("Warning removed", f"Case #{case_number} no longer counts.", "ok"))

    @mod.command(name="history", description="Show every moderation case for a user")
    @app_commands.describe(user="User to look up")
    @sec(Level.MOD)
    async def history(self, interaction: discord.Interaction, user: discord.User):
        rows = await self.bot.db.fetch(
            "SELECT case_number, action, moderator_id, reason, created_at FROM mod_cases "
            "WHERE guild_id=$1 AND user_id=$2 ORDER BY case_number DESC LIMIT 15",
            interaction.guild.id, user.id,
        )
        if not rows:
            return await reply(interaction, content=f"No moderation history for {user}.")
        lines = [f"**#{r['case_number']}** `{r['action']}` {discord.utils.format_dt(r['created_at'], 'R')} by <@{r['moderator_id']}> - {clip(r['reason'] or '', 100)}" for r in rows]
        await reply(interaction, embed=make_embed(f"History for {user}", clip("\n".join(lines), 4000), "info"))

    @mod.command(name="case", description="Look up a single moderation case")
    @app_commands.describe(case_number="The case number")
    @sec(Level.MOD)
    async def case(self, interaction: discord.Interaction, case_number: app_commands.Range[int, 1, 10_000_000]):
        r = await self.bot.db.fetchrow("SELECT * FROM mod_cases WHERE guild_id=$1 AND case_number=$2", interaction.guild.id, case_number)
        if r is None:
            return await reply(interaction, content="No such case.")
        embed = make_embed(f"Case #{r['case_number']} - {r['action']}", kind="info")
        embed.add_field(name="User", value=f"<@{r['user_id']}> (`{r['user_id']}`)")
        embed.add_field(name="Moderator", value=f"<@{r['moderator_id']}>")
        embed.add_field(name="When", value=discord.utils.format_dt(r["created_at"], "F"))
        embed.add_field(name="Reason", value=clip(r["reason"] or "None", 1000), inline=False)
        if r["expires_at"]:
            embed.add_field(name="Expires", value=discord.utils.format_dt(r["expires_at"], "R"))
        embed.add_field(name="Active", value="yes" if r["active"] else "no")
        await reply(interaction, embed=embed)

    # -------------------------------------------------------------- purge
    @mod.command(name="purge", description="Bulk-delete recent messages in this channel")
    @app_commands.describe(amount="How many messages to scan (1-500)", user="Only delete this member's messages", bots_only="Only delete bot messages", with_attachments="Only delete messages with attachments")
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @sec(Level.MOD)
    async def purge(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500],
        user: Optional[discord.Member] = None, bots_only: bool = False, with_attachments: bool = False,
    ):
        channel = interaction.channel
        if not hasattr(channel, "purge"):
            return await reply(interaction, content="I can't purge this kind of channel.")
        if amount >= 50 and not await confirm(interaction, "Confirm purge", f"Scan the last **{amount}** messages here and delete matches?"):
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        def matches(m: discord.Message) -> bool:
            return (
                not m.pinned
                and (user is None or m.author.id == user.id)
                and (not bots_only or m.author.bot)
                and (not with_attachments or bool(m.attachments))
            )

        deleted = await channel.purge(limit=amount, check=matches, reason=f"Purge by {interaction.user} ({interaction.user.id})")
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "mod:purge", f"<#{channel.id}>", f"{len(deleted)} message(s) deleted")
        await reply(interaction, embed=make_embed("Purge complete", f"Deleted **{len(deleted)}** message(s). Messages older than 14 days can't be bulk-deleted.", "ok"))

    # ----------------------------------------------------- channel control
    @mod.command(name="slowmode", description="Set slowmode in this channel")
    @app_commands.describe(seconds="Seconds between messages (0 turns it off, max 21600)")
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @sec(Level.MOD)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await reply(interaction, content="Slowmode only works in text channels and threads.")
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user} ({interaction.user.id})")
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "mod:slowmode", f"<#{channel.id}>", f"{seconds}s")
        await reply(interaction, content="Slowmode disabled." if seconds == 0 else f"Slowmode set to {seconds}s.")

    @mod.command(name="lock", description="Stop @everyone from sending messages in this channel")
    @app_commands.describe(reason="Reason")
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @sec(Level.MOD)
    async def lock(self, interaction: discord.Interaction, reason: Optional[Reason] = None):
        await self._set_lock(interaction, True, reason)

    @mod.command(name="unlock", description="Let @everyone send messages in this channel again")
    @app_commands.describe(reason="Reason")
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @sec(Level.MOD)
    async def unlock(self, interaction: discord.Interaction, reason: Optional[Reason] = None):
        await self._set_lock(interaction, False, reason)

    async def _set_lock(self, interaction, locked: bool, reason):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await reply(interaction, content="Only regular text channels can be locked.")
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False if locked else None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=clip(f"{'Lock' if locked else 'Unlock'} by {interaction.user} ({interaction.user.id}): {reason or 'No reason'}", 500))
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "mod:lock" if locked else "mod:unlock", f"<#{channel.id}>", reason)
        await reply(interaction, content=("🔒 Channel locked." if locked else "🔓 Channel unlocked."))

    @mod.command(name="nick", description="Change or reset a member's nickname")
    @app_commands.describe(member="Member", nickname="New nickname (leave empty to reset)")
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    @sec(Level.MOD)
    async def nick(self, interaction: discord.Interaction, member: discord.Member, nickname: Optional[app_commands.Range[str, 1, 32]] = None):
        if err := await self._guard(interaction, member):
            return await reply(interaction, content=err)
        await member.edit(nick=nickname, reason=f"Nick change by {interaction.user} ({interaction.user.id})")
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "mod:nick", f"{member} ({member.id})", nickname or "reset")
        await reply(interaction, content="Nickname updated.")

    # ----------------------------------------------------- tempban expiry
    @tasks.loop(seconds=30)
    async def tempban_loop(self):
        try:
            rows = await self.bot.db.fetch(
                "SELECT id, guild_id, user_id, case_number FROM mod_cases "
                "WHERE action='tempban' AND active AND expires_at <= now() LIMIT 25"
            )
            for r in rows:
                guild = self.bot.get_guild(r["guild_id"])
                if guild is None:
                    continue
                try:
                    await guild.unban(discord.Object(id=r["user_id"]), reason=f"Tempban (case #{r['case_number']}) expired")
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.warning("Could not unban %s in %s", r["user_id"], r["guild_id"], exc_info=True)
                    continue
                await self.bot.db.execute("UPDATE mod_cases SET active = FALSE WHERE id = $1", r["id"])
                await self.bot.security.audit(r["guild_id"], None, "mod:tempban-expired", f"<@{r['user_id']}>", f"Case #{r['case_number']}")
        except Exception:
            log.exception("tempban loop failed")

    @tempban_loop.before_loop
    async def _before_tempban(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
