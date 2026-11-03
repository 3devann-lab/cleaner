"""/security - access control, blacklist, lockdown, audit log and anti-raid settings."""
import logging
from datetime import timedelta
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.access import LEVEL_NAMES, Level
from core.helpers import format_duration, parse_duration
from core.security import sec
from core.ui import clip, confirm, make_embed, reply

log = logging.getLogger(__name__)

MAX_RULES_PER_GUILD = 300
LEVEL_CHOICES = {"trusted": Level.TRUSTED, "mod": Level.MOD, "admin": Level.ADMIN}


class SecurityAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --------------------------------------------------------- autocomplete
    async def _command_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = current.lower()
        keys = [k for k in self.bot.security.known_keys() if needle in k.lower()]
        return [app_commands.Choice(name=clip(k, 100), value=clip(k, 100)) for k in keys[:25]]

    async def _real_command_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = current.lower()
        names = [n for n in sorted(self.bot.security.command_objects()) if needle in n.lower()]
        return [app_commands.Choice(name=clip(n, 100), value=clip(n, 100)) for n in names[:25]]

    security = app_commands.Group(
        name="security",
        description="Access control, blacklist, lockdown and audit tools",
        guild_only=True,
    )

    # ------------------------------------------------------------ check
    @security.command(name="check", description="Check whether someone can use a command (with a full explanation)")
    @app_commands.describe(
        command="The command to test",
        user="Member to test (staff only; defaults to you)",
        channel="Channel to test in (defaults to this one)",
    )
    @app_commands.autocomplete(command=_real_command_autocomplete)
    @sec(Level.EVERYONE)
    async def check(
        self,
        interaction: discord.Interaction,
        command: str,
        user: Optional[discord.Member] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        sm = self.bot.security
        target = user or interaction.user
        if target.id != interaction.user.id:
            if await sm.member_level(interaction.user) < Level.MOD:
                return await reply(interaction, content="Only moderators can check other people.")
        cmd = sm.command_objects().get(command.strip())
        if cmd is None:
            return await reply(
                interaction,
                content="I don't know that command. Pick one from the autocomplete list.",
            )
        decision = await sm.decide(target, cmd, channel or interaction.channel)
        embed = make_embed(
            ("✅ Allowed" if decision.allowed else "⛔ Denied") + f" - {command.strip()}",
            "\n".join(decision.trace),
            "ok" if decision.allowed else "err",
        )
        embed.set_footer(text=f"Checked for {target} ({target.id})")
        await reply(interaction, embed=embed)

    # -------------------------------------------------------- allow / deny
    async def _add_rule(
        self,
        interaction: discord.Interaction,
        effect: str,
        command: str,
        role: Optional[discord.Role],
        user: Optional[discord.Member],
        channel: Optional[discord.abc.GuildChannel],
        duration: Optional[str],
    ):
        sm = self.bot.security
        picked = [(t, o) for t, o in (("role", role), ("user", user), ("channel", channel)) if o is not None]
        if len(picked) != 1:
            return await reply(interaction, content="Pick exactly ONE of: role, user or channel.")
        target_type, target = picked[0]
        command = command.strip()
        if command not in sm.known_keys():
            return await reply(interaction, content="Unknown command. Pick one from the autocomplete list.")

        expires = None
        if duration:
            try:
                delta = parse_duration(duration, min_seconds=60, max_seconds=90 * 86400)
            except ValueError as exc:
                return await reply(interaction, content=str(exc))
            expires = discord.utils.utcnow() + delta

        if target_type == "user" and (target.id == interaction.guild.owner_id or target.id in config.OWNER_IDS):
            return await reply(interaction, content="Rules never apply to owners, so that would do nothing.")

        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM command_rules WHERE guild_id = $1", interaction.guild.id
        )
        if count >= MAX_RULES_PER_GUILD:
            return await reply(interaction, content=f"This server already has {MAX_RULES_PER_GUILD} rules. Remove some first.")

        await self.bot.db.execute(
            "INSERT INTO command_rules (guild_id, command, target_type, target_id, effect, created_by, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (guild_id, command, target_type, target_id) "
            "DO UPDATE SET effect = EXCLUDED.effect, created_by = EXCLUDED.created_by, "
            "expires_at = EXCLUDED.expires_at, created_at = now()",
            interaction.guild.id, command, target_type, target.id, effect, interaction.user.id, expires,
        )
        sm.invalidate(interaction.guild.id)
        label = {"role": f"<@&{target.id}>", "user": f"<@{target.id}>", "channel": f"<#{target.id}>"}[target_type]
        when = f" until {discord.utils.format_dt(expires, 'f')}" if expires else ""
        await sm.audit(
            interaction.guild.id, interaction.user.id, f"rule:{effect}",
            f"`{command}` -> {target_type} {target.id}", f"{effect.upper()} `{command}` for {label}{when}",
        )
        await reply(
            interaction,
            embed=make_embed(
                f"Rule saved: {effect.upper()}",
                f"`{command}` for {target_type} {label}{when}",
                "ok" if effect == "allow" else "warn",
            ),
        )

    @security.command(name="allow", description="Allow a role, user or channel to use a command")
    @app_commands.describe(
        command="Command, group, cog:name or * (everything)",
        role="Role to allow", user="Member to allow", channel="Channel (or category) where it is allowed",
        duration="Optional expiry, e.g. 2h or 7d (temporary access)",
    )
    @app_commands.autocomplete(command=_command_autocomplete)
    @sec(Level.ADMIN)
    async def allow(
        self, interaction: discord.Interaction, command: str,
        role: Optional[discord.Role] = None, user: Optional[discord.Member] = None,
        channel: Optional[discord.abc.GuildChannel] = None, duration: Optional[str] = None,
    ):
        await self._add_rule(interaction, "allow", command, role, user, channel, duration)

    @security.command(name="deny", description="Deny a role, user or channel from using a command")
    @app_commands.describe(
        command="Command, group, cog:name or * (everything)",
        role="Role to deny (use @everyone for a default deny)", user="Member to deny",
        channel="Channel (or category) where it is disabled",
        duration="Optional expiry, e.g. 2h or 7d",
    )
    @app_commands.autocomplete(command=_command_autocomplete)
    @sec(Level.ADMIN)
    async def deny(
        self, interaction: discord.Interaction, command: str,
        role: Optional[discord.Role] = None, user: Optional[discord.Member] = None,
        channel: Optional[discord.abc.GuildChannel] = None, duration: Optional[str] = None,
    ):
        await self._add_rule(interaction, "deny", command, role, user, channel, duration)

    @security.command(name="remove", description="Remove a rule (or every rule for a command)")
    @app_commands.describe(
        command="Command the rule is attached to",
        role="Role the rule targets", user="Member the rule targets", channel="Channel the rule targets",
    )
    @app_commands.autocomplete(command=_command_autocomplete)
    @sec(Level.ADMIN)
    async def remove(
        self, interaction: discord.Interaction, command: str,
        role: Optional[discord.Role] = None, user: Optional[discord.Member] = None,
        channel: Optional[discord.abc.GuildChannel] = None,
    ):
        picked = [(t, o) for t, o in (("role", role), ("user", user), ("channel", channel)) if o is not None]
        if len(picked) > 1:
            return await reply(interaction, content="Pick at most ONE of: role, user or channel.")
        command = command.strip()
        if picked:
            target_type, target = picked[0]
            result = await self.bot.db.execute(
                "DELETE FROM command_rules WHERE guild_id=$1 AND command=$2 AND target_type=$3 AND target_id=$4",
                interaction.guild.id, command, target_type, target.id,
            )
            detail = f"{target_type} {target.id}"
        else:
            if not await confirm(interaction, "Remove ALL rules?", f"This deletes every rule attached to `{command}`."):
                return
            result = await self.bot.db.execute(
                "DELETE FROM command_rules WHERE guild_id=$1 AND command=$2", interaction.guild.id, command
            )
            detail = "all targets"
        removed = int(result.split()[-1])
        self.bot.security.invalidate(interaction.guild.id)
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "rule:remove", f"`{command}`", f"{removed} rule(s) removed ({detail})")
        await reply(interaction, embed=make_embed("Rules removed", f"{removed} rule(s) removed for `{command}`.", "ok"))

    @security.command(name="rules", description="List the custom command rules in this server")
    @app_commands.describe(command="Only show rules for this command")
    @app_commands.autocomplete(command=_command_autocomplete)
    @sec(Level.ADMIN)
    async def rules(self, interaction: discord.Interaction, command: Optional[str] = None):
        rows = await self.bot.db.fetch(
            "SELECT command, target_type, target_id, effect, expires_at FROM command_rules "
            "WHERE guild_id = $1 AND ($2::text IS NULL OR command = $2) "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY command, effect LIMIT 40",
            interaction.guild.id, command.strip() if command else None,
        )
        if not rows:
            return await reply(interaction, embed=make_embed("No custom rules", "Everything uses default access levels.", "info"))
        lines = []
        for r in rows:
            ref = {"role": f"<@&{r['target_id']}>", "user": f"<@{r['target_id']}>", "channel": f"<#{r['target_id']}>"}[r["target_type"]]
            exp = f" (until {discord.utils.format_dt(r['expires_at'], 'R')})" if r["expires_at"] else ""
            icon = "✅" if r["effect"] == "allow" else "⛔"
            lines.append(f"{icon} `{r['command']}` - {r['target_type']} {ref}{exp}")
        await reply(interaction, embed=make_embed("Command rules", clip("\n".join(lines), 4000), "info"))

    # --------------------------------------------------------- blacklist
    @security.command(name="blacklist", description="Block someone from using this bot in this server")
    @app_commands.describe(user="Person to block", reason="Why", duration="Optional expiry, e.g. 7d")
    @sec(Level.ADMIN)
    async def blacklist(
        self, interaction: discord.Interaction, user: discord.User,
        reason: Optional[app_commands.Range[str, 1, 200]] = None, duration: Optional[str] = None,
    ):
        guild, sm = interaction.guild, self.bot.security
        if user.id == interaction.user.id or user.bot:
            return await reply(interaction, content="You can't blacklist yourself or a bot.")
        if user.id == guild.owner_id or user.id in config.OWNER_IDS:
            return await reply(interaction, content="Owners can't be blacklisted.")
        member = guild.get_member(user.id)
        if member and interaction.user.id != guild.owner_id:
            if await sm.member_level(member) >= await sm.member_level(interaction.user):
                return await reply(interaction, content="You can't blacklist someone with equal or higher access than you.")
        expires = None
        if duration:
            try:
                expires = discord.utils.utcnow() + parse_duration(duration, min_seconds=60, max_seconds=365 * 86400)
            except ValueError as exc:
                return await reply(interaction, content=str(exc))
        await self.bot.db.execute(
            "INSERT INTO blacklist (guild_id, user_id, reason, added_by, expires_at) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET reason=EXCLUDED.reason, "
            "added_by=EXCLUDED.added_by, expires_at=EXCLUDED.expires_at, created_at=now()",
            guild.id, user.id, reason, interaction.user.id, expires,
        )
        sm.invalidate(guild.id)
        await sm.audit(guild.id, interaction.user.id, "blacklist:add", f"<@{user.id}>", reason or "No reason given")
        await reply(interaction, embed=make_embed("User blacklisted", f"<@{user.id}> can no longer use this bot here." + (f"\nExpires {discord.utils.format_dt(expires, 'R')}" if expires else ""), "warn"))

    @security.command(name="unblacklist", description="Let a blacklisted person use the bot again")
    @app_commands.describe(user="Person to unblock")
    @sec(Level.ADMIN)
    async def unblacklist(self, interaction: discord.Interaction, user: discord.User):
        result = await self.bot.db.execute("DELETE FROM blacklist WHERE guild_id=$1 AND user_id=$2", interaction.guild.id, user.id)
        self.bot.security.invalidate(interaction.guild.id)
        if result.endswith(" 0"):
            return await reply(interaction, content="That user isn't blacklisted here.")
        await self.bot.security.audit(interaction.guild.id, interaction.user.id, "blacklist:remove", f"<@{user.id}>")
        await reply(interaction, embed=make_embed("Blacklist removed", f"<@{user.id}> can use the bot again.", "ok"))

    @security.command(name="blacklisted", description="List blacklisted people in this server")
    @sec(Level.ADMIN)
    async def blacklisted(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch(
            "SELECT user_id, reason, expires_at FROM blacklist WHERE guild_id=$1 "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC LIMIT 40",
            interaction.guild.id,
        )
        if not rows:
            return await reply(interaction, embed=make_embed("Blacklist is empty", kind="info"))
        lines = [
            f"<@{r['user_id']}> - {clip(r['reason'] or 'No reason', 80)}"
            + (f" (expires {discord.utils.format_dt(r['expires_at'], 'R')})" if r["expires_at"] else "")
            for r in rows
        ]
        await reply(interaction, embed=make_embed("Blacklisted users", clip("\n".join(lines), 4000), "warn"))

    # ---------------------------------------------------------- lockdown
    @security.command(name="lockdown", description="Only staff can use bot commands while lockdown is on")
    @app_commands.describe(state="on or off")
    @sec(Level.ADMIN)
    async def lockdown(self, interaction: discord.Interaction, state: Literal["on", "off"]):
        sm = self.bot.security
        await sm.ensure_settings_row(interaction.guild.id)
        await self.bot.db.execute("UPDATE guild_settings SET lockdown=$2 WHERE guild_id=$1", interaction.guild.id, state == "on")
        sm.invalidate(interaction.guild.id)
        await sm.audit(interaction.guild.id, interaction.user.id, f"lockdown:{state}")
        await reply(interaction, embed=make_embed(f"Lockdown {state.upper()}", "Only staff can use commands." if state == "on" else "Commands are available to everyone again.", "warn" if state == "on" else "ok"))

    # ------------------------------------------------------ staff roles
    @security.command(name="setrole", description="Give a role a bot access level (trusted / mod / admin)")
    @app_commands.describe(role="Role to configure", level="Access level, or none to remove")
    @sec(Level.ADMIN)
    async def setrole(self, interaction: discord.Interaction, role: discord.Role, level: Literal["trusted", "mod", "admin", "none"]):
        guild, sm = interaction.guild, self.bot.security
        if role.is_default() or role.managed:
            return await reply(interaction, content="That role can't be used as a staff role.")
        if level == "none":
            await self.bot.db.execute("DELETE FROM guild_roles WHERE guild_id=$1 AND role_id=$2", guild.id, role.id)
        else:
            wanted = LEVEL_CHOICES[level]
            mine = await sm.member_level(interaction.user)
            if wanted > mine:
                return await reply(interaction, content="You can't grant a higher level than your own.")
            await self.bot.db.execute(
                "INSERT INTO guild_roles (guild_id, role_id, level) VALUES ($1,$2,$3) "
                "ON CONFLICT (guild_id, role_id) DO UPDATE SET level = EXCLUDED.level",
                guild.id, role.id, int(wanted),
            )
        sm.invalidate(guild.id)
        await sm.audit(guild.id, interaction.user.id, "role-level", f"<@&{role.id}>", f"Level set to {level}")
        await reply(interaction, embed=make_embed("Role updated", f"{role.mention} is now **{level}**.", "ok"))

    @security.command(name="roles", description="Show which roles have bot access levels")
    @sec(Level.ADMIN)
    async def roles(self, interaction: discord.Interaction):
        policy = await self.bot.security.get_policy(interaction.guild.id, force=True)
        if not policy.role_levels:
            return await reply(interaction, content="No roles configured. Staff are detected from Discord permissions only.")
        lines = [f"<@&{rid}> - **{LEVEL_NAMES[lv]}**" for rid, lv in sorted(policy.role_levels.items(), key=lambda kv: -kv[1])]
        await reply(interaction, embed=make_embed("Bot access roles", "\n".join(lines), "info"))

    # --------------------------------------------------------- audit & log
    @security.command(name="audit", description="View the security audit log")
    @app_commands.describe(user="Only entries involving this user")
    @sec(Level.ADMIN)
    async def audit(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        rows = await self.bot.db.fetch(
            "SELECT actor_id, action, target, detail, created_at FROM audit_log WHERE guild_id=$1 "
            "AND ($2::bigint IS NULL OR actor_id = $2) ORDER BY id DESC LIMIT 15",
            interaction.guild.id, user.id if user else None,
        )
        if not rows:
            return await reply(interaction, content="Nothing in the audit log yet.")
        lines = []
        for r in rows:
            actor = f"<@{r['actor_id']}>" if r["actor_id"] else "bot"
            lines.append(f"{discord.utils.format_dt(r['created_at'], 'R')} **{clip(r['action'], 40)}** by {actor}" + (f" -> {clip(r['target'], 60)}" if r["target"] else ""))
        await reply(interaction, embed=make_embed("Security audit log", clip("\n".join(lines), 4000), "info"))

    @security.command(name="logchannel", description="Set where security and moderation events are logged")
    @app_commands.describe(channel="Log channel (leave empty to turn logging off)")
    @sec(Level.ADMIN)
    async def logchannel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        sm = self.bot.security
        if channel is not None:
            me = interaction.guild.me
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await reply(interaction, content="I need View Channel, Send Messages and Embed Links there.")
        await sm.ensure_settings_row(interaction.guild.id)
        await self.bot.db.execute("UPDATE guild_settings SET log_channel_id=$2 WHERE guild_id=$1", interaction.guild.id, channel.id if channel else None)
        sm.invalidate(interaction.guild.id)
        await sm.audit(interaction.guild.id, interaction.user.id, "logchannel", channel.mention if channel else "disabled")
        await reply(interaction, content=f"Logging to {channel.mention}." if channel else "Logging turned off.")

    # ---------------------------------------------------------- anti-raid
    @security.command(name="antiraid", description="Configure join-flood protection and the minimum account age")
    @app_commands.describe(
        enabled="Turn raid detection on or off",
        threshold="Joins within the window that count as a raid (3-50)",
        window="Window in seconds (5-300)",
        action="What to do to joiners during a raid",
        min_account_age_days="Act on accounts younger than this many days (0 = off)",
    )
    @sec(Level.ADMIN)
    async def antiraid(
        self, interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        threshold: Optional[app_commands.Range[int, 3, 50]] = None,
        window: Optional[app_commands.Range[int, 5, 300]] = None,
        action: Optional[Literal["alert", "timeout", "kick"]] = None,
        min_account_age_days: Optional[app_commands.Range[int, 0, 365]] = None,
    ):
        sm = self.bot.security
        await sm.ensure_settings_row(interaction.guild.id)
        changes = {"antiraid_enabled": enabled, "raid_threshold": threshold, "raid_window": window, "raid_action": action, "min_account_age_days": min_account_age_days}
        for column, value in changes.items():
            if value is not None:
                # column names come from the fixed dict above, never from user input
                await self.bot.db.execute(f"UPDATE guild_settings SET {column} = $2 WHERE guild_id = $1", interaction.guild.id, value)
        if any(v is not None for v in changes.values()):
            sm.invalidate(interaction.guild.id)
            await sm.audit(interaction.guild.id, interaction.user.id, "antiraid:update", detail=", ".join(f"{k}={v}" for k, v in changes.items() if v is not None))
        s = await sm.settings(interaction.guild.id)
        embed = make_embed("Anti-raid settings", kind="info")
        embed.add_field(name="Raid detection", value="ON" if s["antiraid_enabled"] else "OFF")
        embed.add_field(name="Trigger", value=f"{s['raid_threshold']} joins / {s['raid_window']}s")
        embed.add_field(name="Action", value=s["raid_action"])
        embed.add_field(name="Min account age", value=f"{s['min_account_age_days']} day(s)" if s["min_account_age_days"] else "off")
        await reply(interaction, embed=embed)

    @security.command(name="overview", description="Summary of this server's security setup")
    @sec(Level.ADMIN)
    async def overview(self, interaction: discord.Interaction):
        sm, gid = self.bot.security, interaction.guild.id
        policy = await sm.get_policy(gid, force=True)
        s = await sm.settings(gid)
        embed = make_embed("Security overview", kind="info")
        embed.add_field(name="Lockdown", value="ON" if policy.lockdown else "off")
        embed.add_field(name="Custom rules", value=str(len(policy.rules)))
        embed.add_field(name="Blacklisted", value=str(len(policy.blacklisted)))
        embed.add_field(name="Staff roles", value=str(len(policy.role_levels)))
        embed.add_field(name="Log channel", value=f"<#{policy.log_channel_id}>" if policy.log_channel_id else "not set")
        embed.add_field(name="Anti-raid", value=("ON" if s["antiraid_enabled"] else "off"))
        await reply(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SecurityAdmin(bot))
