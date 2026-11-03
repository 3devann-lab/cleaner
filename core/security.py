"""Security manager: connects the pure permission engine to Discord.

Every slash command and context-menu command passes through
``SecureTree.interaction_check`` BEFORE its code runs, so a command can never
execute unless ``core.access.evaluate`` allows it.
"""
import logging
import secrets
import time
from collections import defaultdict, deque
from typing import Optional

import discord
from discord import app_commands

import config
from core.access import (
    CommandInfo,
    Decision,
    GuildPolicy,
    Level,
    LEVEL_NAMES,
    Rule,
    Subject,
    evaluate,
)
from core.ui import COLOR

log = logging.getLogger(__name__)

__all__ = ["sec", "Level", "LEVEL_NAMES", "SecurityManager", "SecureTree", "command_info"]

POLICY_TTL = 20  # seconds a guild's policy is cached
THROTTLE_MAX, THROTTLE_WINDOW = 6, 10  # max 6 commands / 10 s per user
STRIKE_LIMIT, STRIKE_WINDOW = 8, 60  # 8 denied attempts / 60 s => cooldown
COOLDOWN_SECONDS = 300
PROTECTED_COGS = {"security_admin"}

DEFAULT_SETTINGS = {
    "log_channel_id": None,
    "lockdown": False,
    "welcome_channel_id": None,
    "welcome_message": None,
    "autorole_id": None,
    "antiraid_enabled": False,
    "raid_threshold": 8,
    "raid_window": 20,
    "raid_action": "alert",
    "min_account_age_days": 0,
    "ticket_category_id": None,
    "ticket_staff_role_id": None,
}


def sec(level: Level):
    """Decorator: the default access level required to run a command.

    Place it BELOW the @app_commands.command / @group.command decorator.
    """

    def decorator(func):
        func.__sec_level__ = level
        return func

    return decorator


def command_info(cmd) -> CommandInfo:
    """Describe an app command (slash, group or context menu) for the engine."""
    if isinstance(cmd, (app_commands.Command, app_commands.Group)):
        parts = tuple(cmd.qualified_name.split())
    else:  # ContextMenu names may contain spaces, e.g. "Bookmark Message"
        parts = (cmd.name,)
    callback = getattr(cmd, "callback", None)
    module = getattr(callback, "__module__", None) or getattr(cmd, "module", None) or ""
    cog = module.rsplit(".", 1)[-1] if module else ""
    level = None
    node = cmd
    while node is not None and level is None:
        level = getattr(getattr(node, "callback", None), "__sec_level__", None)
        node = getattr(node, "parent", None)
    return CommandInfo(
        parts=parts,
        cog=cog,
        required=level if level is not None else Level.EVERYONE,
        protected=cog in PROTECTED_COGS,
    )


class SecurityManager:
    def __init__(self, bot):
        self.bot = bot
        self._policies = {}
        self._settings = {}
        self._global_bl = {}
        self._calls = defaultdict(deque)
        self._denials = defaultdict(deque)
        self._cooldowns = {}
        self._audit_seen = {}

    # ------------------------------------------------------------ data loading
    async def load_global(self) -> None:
        rows = await self.bot.db.fetch(
            "SELECT user_id, reason FROM blacklist "
            "WHERE guild_id = 0 AND (expires_at IS NULL OR expires_at > now())"
        )
        self._global_bl = {r["user_id"]: r["reason"] or "" for r in rows}

    @property
    def global_blacklist(self) -> dict:
        return self._global_bl

    def invalidate(self, guild_id: Optional[int] = None) -> None:
        if guild_id is None:
            self._policies.clear()
            self._settings.clear()
        else:
            self._policies.pop(guild_id, None)
            self._settings.pop(guild_id, None)

    async def get_policy(self, guild_id: int, force: bool = False) -> GuildPolicy:
        cached = self._policies.get(guild_id)
        if cached and not force and time.monotonic() - cached[0] < POLICY_TTL:
            return cached[1]
        db = self.bot.db
        settings = await db.fetchrow(
            "SELECT lockdown, log_channel_id FROM guild_settings WHERE guild_id = $1", guild_id
        )
        rule_rows = await db.fetch(
            "SELECT command, target_type, target_id, effect, "
            "EXTRACT(EPOCH FROM expires_at) AS exp FROM command_rules "
            "WHERE guild_id = $1 AND (expires_at IS NULL OR expires_at > now())",
            guild_id,
        )
        bl_rows = await db.fetch(
            "SELECT user_id, reason FROM blacklist "
            "WHERE guild_id = $1 AND (expires_at IS NULL OR expires_at > now())",
            guild_id,
        )
        role_rows = await db.fetch(
            "SELECT role_id, level FROM guild_roles WHERE guild_id = $1", guild_id
        )
        policy = GuildPolicy(
            lockdown=bool(settings["lockdown"]) if settings else False,
            rules=[
                Rule(
                    r["command"],
                    r["target_type"],
                    r["target_id"],
                    r["effect"],
                    float(r["exp"]) if r["exp"] is not None else None,
                )
                for r in rule_rows
            ],
            blacklisted={r["user_id"]: r["reason"] or "" for r in bl_rows},
            role_levels={r["role_id"]: Level(r["level"]) for r in role_rows},
            log_channel_id=settings["log_channel_id"] if settings else None,
        )
        self._policies[guild_id] = (time.monotonic(), policy)
        return policy

    async def settings(self, guild_id: int) -> dict:
        cached = self._settings.get(guild_id)
        if cached and time.monotonic() - cached[0] < POLICY_TTL:
            return cached[1]
        row = await self.bot.db.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
        data = dict(DEFAULT_SETTINGS)
        if row:
            data.update(dict(row))
        self._settings[guild_id] = (time.monotonic(), data)
        return data

    async def ensure_settings_row(self, guild_id: int) -> None:
        await self.bot.db.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id
        )

    # ------------------------------------------------------------- subjects
    def build_subject(self, member: discord.Member, channel, policy: GuildPolicy) -> Subject:
        guild = member.guild
        is_bot_owner = member.id in config.OWNER_IDS
        is_guild_owner = member.id == guild.owner_id
        role_ids = frozenset(r.id for r in member.roles if r.id != guild.id)

        level = Level.EVERYONE
        for rid in role_ids:
            level = max(level, policy.role_levels.get(rid, Level.EVERYONE))
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            level = max(level, Level.ADMIN)
        elif perms.kick_members or perms.ban_members or perms.manage_messages or perms.moderate_members:
            level = max(level, Level.MOD)
        if is_guild_owner:
            level = max(level, Level.SERVER_OWNER)
        if is_bot_owner:
            level = Level.BOT_OWNER

        channel_ids = set()
        if channel is not None:
            channel_ids.add(channel.id)
            if isinstance(channel, discord.Thread):
                channel_ids.add(channel.parent_id)
                if channel.parent is not None and channel.parent.category_id:
                    channel_ids.add(channel.parent.category_id)
            elif getattr(channel, "category_id", None):
                channel_ids.add(channel.category_id)

        return Subject(
            user_id=member.id,
            role_ids=role_ids,
            everyone_role_id=guild.id,
            channel_ids=frozenset(channel_ids),
            level=level,
            is_bot_owner=is_bot_owner,
            is_guild_owner=is_guild_owner,
        )

    async def member_level(self, member: discord.Member) -> Level:
        policy = await self.get_policy(member.guild.id)
        return self.build_subject(member, None, policy).level

    # ------------------------------------------------------ command lookups
    def command_objects(self) -> dict:
        tree = self.bot.tree
        found = {}
        for cmd in tree.walk_commands():
            found[cmd.qualified_name] = cmd
        for kind in (discord.AppCommandType.message, discord.AppCommandType.user):
            for cm in tree.get_commands(type=kind):
                found[cm.name] = cm
        return found

    def known_keys(self) -> list:
        keys = {"*"}
        for name, cmd in self.command_objects().items():
            keys.add(name)
            cog = command_info(cmd).cog
            if cog:
                keys.add(f"cog:{cog}")
        return sorted(keys)

    async def decide(self, member: discord.Member, cmd, channel=None) -> Decision:
        policy = await self.get_policy(member.guild.id)
        subject = self.build_subject(member, channel, policy)
        return evaluate(subject, command_info(cmd), policy, self._global_bl, time.time())

    # ------------------------------------------------------------ main check
    def _throttled(self, user_id: int, now: float) -> bool:
        calls = self._calls[user_id]
        while calls and now - calls[0] > THROTTLE_WINDOW:
            calls.popleft()
        if len(calls) >= THROTTLE_MAX:
            return True
        calls.append(now)
        return False

    async def check_interaction(self, interaction: discord.Interaction) -> bool:
        # Autocomplete never executes anything; the real command is checked on submit.
        if interaction.type is discord.InteractionType.autocomplete:
            return True
        cmd = interaction.command
        if cmd is None:
            return True  # unknown command: discord.py reports it
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._deny(interaction, "This bot's commands only work inside servers.")
            return False

        user = interaction.user
        now = time.time()
        is_owner = user.id in config.OWNER_IDS

        if not is_owner and self._throttled(user.id, now):
            await self._deny(interaction, "You're sending commands too fast. Wait a few seconds.")
            return False

        key = (interaction.guild.id, user.id)
        until = self._cooldowns.get(key)
        if until and until > now and not is_owner:
            minutes = max(1, int((until - now) // 60) + 1)
            await self._deny(
                interaction,
                f"Too many denied attempts. You're locked out of bot commands for about {minutes} min.",
            )
            return False

        info = command_info(cmd)
        policy = await self.get_policy(interaction.guild.id)
        subject = self.build_subject(user, interaction.channel, policy)
        decision = evaluate(subject, info, policy, self._global_bl, now)
        if decision.allowed:
            return True

        await self._record_denial(interaction, info, decision, key, now)
        await self._deny(interaction, decision.reason)
        return False

    async def check_component(self, interaction: discord.Interaction) -> bool:
        """Light check for buttons/menus (polls, giveaways, tickets)."""
        user = interaction.user
        if user.id in config.OWNER_IDS:
            return True
        if interaction.guild is None:
            await self._deny(interaction, "This only works inside a server.")
            return False
        if user.id in self._global_bl:
            await self._deny(interaction, "You are blocked from using this bot.")
            return False
        policy = await self.get_policy(interaction.guild.id)
        if user.id in policy.blacklisted and user.id != interaction.guild.owner_id:
            await self._deny(interaction, "You are blocked from using this bot in this server.")
            return False
        if self._throttled(user.id, time.time()):
            await self._deny(interaction, "Slow down a little.")
            return False
        return True

    async def _record_denial(self, interaction, info: CommandInfo, decision: Decision, key, now: float):
        if decision.code == "blacklist":
            return
        strikes = self._denials[key]
        while strikes and now - strikes[0] > STRIKE_WINDOW:
            strikes.popleft()
        strikes.append(now)

        # Log privileged attempts (deduplicated to one per minute per user+command)
        if info.required >= Level.MOD:
            seen_key = (key, info.name)
            if now - self._audit_seen.get(seen_key, 0) > 60:
                self._audit_seen[seen_key] = now
                await self.audit(
                    interaction.guild.id,
                    interaction.user.id,
                    "denied",
                    f"/{info.name}",
                    f"Blocked ({decision.code}) in <#{interaction.channel_id}>",
                )

        if len(strikes) >= STRIKE_LIMIT:
            strikes.clear()
            self._cooldowns[key] = now + COOLDOWN_SECONDS
            await self.audit(
                interaction.guild.id,
                interaction.user.id,
                "auto-cooldown",
                f"<@{interaction.user.id}>",
                f"{STRIKE_LIMIT} denied attempts within {STRIKE_WINDOW}s. "
                f"Locked out for {COOLDOWN_SECONDS // 60} minutes.",
            )

    async def _deny(self, interaction: discord.Interaction, reason: str) -> None:
        embed = discord.Embed(title="⛔ Access denied", description=reason, color=COLOR["err"])
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.debug("Could not deliver denial message", exc_info=True)

    # ------------------------------------------------------------------ audit
    async def audit(self, guild_id: int, actor_id, action: str, target=None, detail=None) -> None:
        try:
            await self.bot.db.execute(
                "INSERT INTO audit_log (guild_id, actor_id, action, target, detail) "
                "VALUES ($1, $2, $3, $4, $5)",
                guild_id,
                actor_id,
                action[:100],
                str(target)[:200] if target is not None else None,
                (detail or "")[:1000] or None,
            )
        except Exception:
            log.exception("Failed to write audit log")
        try:
            policy = await self.get_policy(guild_id)
            if not policy.log_channel_id:
                return
            channel = self.bot.get_channel(policy.log_channel_id)
            if channel is None or not hasattr(channel, "send"):
                return
            embed = discord.Embed(
                title=f"🛡️ {action}", color=COLOR["info"], timestamp=discord.utils.utcnow()
            )
            if actor_id:
                embed.add_field(name="Actor", value=f"<@{actor_id}> (`{actor_id}`)", inline=True)
            if target is not None:
                embed.add_field(name="Target", value=str(target)[:200], inline=True)
            if detail:
                embed.description = str(detail)[:1000]
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.debug("Could not post to log channel", exc_info=True)
        except Exception:
            log.exception("Unexpected error while posting audit log")


async def handle_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Slow down. Try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.BotMissingPermissions):
        names = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        message = f"I'm missing permissions to do that: **{names}**."
    elif isinstance(error, app_commands.MissingPermissions):
        names = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
        message = f"You need these permissions: **{names}**."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This only works inside a server."
    elif isinstance(error, app_commands.CheckFailure):
        message = str(error) or "You can't use this command."
    elif isinstance(original, discord.Forbidden):
        message = (
            "Discord refused that action. Check that I have the permission and that "
            "my highest role is above the target's highest role."
        )
    else:
        error_id = secrets.token_hex(3)
        log.error("Unhandled command error [%s]", error_id, exc_info=original)
        message = f"Something went wrong on my side. Reference: `{error_id}`"

    embed = discord.Embed(description=message, color=COLOR["err"])
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


class SecureTree(app_commands.CommandTree):
    """Command tree whose global check enforces the permission engine."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.client.security.check_interaction(interaction)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await handle_app_error(interaction, error)
