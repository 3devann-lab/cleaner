"""The permission engine.

This module is deliberately free of any Discord imports: it works on plain
ids and dataclasses, so the exact logic that guards every command can be
unit-tested anywhere (see tests/test_access.py).

Evaluation order (first decisive step wins):

  1. Global blacklist (set by the bot owner)            -> deny
  2. Server blacklist                                    -> deny
  3. Bot owner / server owner                            -> allow (cannot be locked out)
  4. Server lockdown (only staff may use commands)       -> deny
  5. Channel rules   (deny here / allow only in X)       -> deny
  6. Member rules, most specific command key first:
        user rule  >  role rules  >  @everyone rule
        (deny beats allow inside the same tier)
  7. Default access level required by the command        -> allow / deny

Rule keys are checked from most to least specific, e.g. for ``mod ban``:
    "mod ban"  ->  "mod"  ->  "cog:moderation"  ->  "*"
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class Level(enum.IntEnum):
    EVERYONE = 0
    TRUSTED = 1
    MOD = 2
    ADMIN = 3
    SERVER_OWNER = 4
    BOT_OWNER = 5


LEVEL_NAMES = {
    Level.EVERYONE: "Everyone",
    Level.TRUSTED: "Trusted",
    Level.MOD: "Moderator",
    Level.ADMIN: "Administrator",
    Level.SERVER_OWNER: "Server owner",
    Level.BOT_OWNER: "Bot owner",
}


@dataclass(frozen=True)
class Rule:
    command: str  # "*", "cog:moderation", "mod ban", "mod", ...
    target_type: str  # "user" | "role" | "channel"
    target_id: int
    effect: str  # "allow" | "deny"
    expires_at: Optional[float] = None  # unix seconds, None = permanent


@dataclass(frozen=True)
class CommandInfo:
    parts: tuple  # ("mod", "ban")
    cog: str = ""  # module name, e.g. "moderation"
    required: Level = Level.EVERYONE
    protected: bool = False  # core security commands: admins can never be locked out

    @property
    def name(self) -> str:
        return " ".join(self.parts)


@dataclass
class GuildPolicy:
    lockdown: bool = False
    rules: list = field(default_factory=list)
    blacklisted: dict = field(default_factory=dict)  # user_id -> reason
    role_levels: dict = field(default_factory=dict)  # role_id -> Level
    log_channel_id: Optional[int] = None


@dataclass(frozen=True)
class Subject:
    user_id: int
    role_ids: frozenset  # excludes @everyone
    everyone_role_id: int
    channel_ids: frozenset  # channel + parent thread channel + category
    level: Level
    is_bot_owner: bool = False
    is_guild_owner: bool = False


@dataclass
class Decision:
    allowed: bool
    code: str  # ok | blacklist | lockdown | channel | rule | level
    reason: str  # safe to show to the person who was denied
    trace: list  # detailed step-by-step explanation (for /security check)


def command_keys(info: CommandInfo) -> list:
    """Rule keys for a command, most specific first."""
    keys = [" ".join(info.parts[:i]) for i in range(len(info.parts), 0, -1)]
    if info.cog:
        keys.append(f"cog:{info.cog}")
    keys.append("*")
    return keys


def _member_verdict(rules: list, key: str, subject: Subject):
    scoped = [r for r in rules if r.command == key]
    if not scoped:
        return None
    tiers = (
        (
            "user rule",
            [r for r in scoped if r.target_type == "user" and r.target_id == subject.user_id],
        ),
        (
            "role rule",
            [r for r in scoped if r.target_type == "role" and r.target_id in subject.role_ids],
        ),
        (
            "@everyone rule",
            [
                r
                for r in scoped
                if r.target_type == "role" and r.target_id == subject.everyone_role_id
            ],
        ),
    )
    for label, hits in tiers:
        if any(r.effect == "deny" for r in hits):
            return False, f"{label}: DENY on `{key}`"
        if hits:
            return True, f"{label}: ALLOW on `{key}`"
    return None


def evaluate(
    subject: Subject,
    info: CommandInfo,
    policy: GuildPolicy,
    global_blacklist: dict,
    now: float,
) -> Decision:
    trace: list = []

    def deny(code: str, reason: str, step: str) -> Decision:
        trace.append(f"⛔ {step}")
        return Decision(False, code, reason, trace)

    def allow(step: str) -> Decision:
        trace.append(f"✅ {step}")
        return Decision(True, "ok", "Allowed.", trace)

    uid = subject.user_id

    # 1-2. Blacklists
    if uid in global_blacklist and not subject.is_bot_owner:
        return deny("blacklist", "You are blocked from using this bot.", "Globally blacklisted")
    trace.append("✓ Not globally blacklisted")
    if uid in policy.blacklisted and not (subject.is_bot_owner or subject.is_guild_owner):
        return deny(
            "blacklist",
            "You are blocked from using this bot in this server.",
            "Blacklisted in this server",
        )
    trace.append("✓ Not blacklisted in this server")

    # 3. Owners can never be locked out
    if subject.is_bot_owner:
        return allow("Bot owner: bypasses every rule")
    if subject.is_guild_owner:
        return allow("Server owner: bypasses every rule")

    protected_admin = info.protected and subject.level >= Level.ADMIN

    # 4. Lockdown
    if policy.lockdown and not protected_admin and subject.level < Level.MOD:
        return deny(
            "lockdown",
            "This server is in lockdown. Only staff can use commands right now.",
            "Server lockdown is ON and you are not staff",
        )
    trace.append("✓ No lockdown restriction applies" if policy.lockdown else "✓ Server is not in lockdown")

    rules = [r for r in policy.rules if r.expires_at is None or r.expires_at > now]
    keys = command_keys(info)
    trace.append("Rule keys checked: " + " → ".join(f"`{k}`" for k in keys))

    if protected_admin:
        trace.append("✓ Core security command: admins are exempt from custom rules (anti-lockout)")
    else:
        # 5. Channel rules
        for key in keys:
            ch_rules = [r for r in rules if r.command == key and r.target_type == "channel"]
            if not ch_rules:
                continue
            if any(
                r.effect == "deny" and r.target_id in subject.channel_ids for r in ch_rules
            ):
                return deny(
                    "channel",
                    "This command is disabled in this channel.",
                    f"Channel rule: DENY on `{key}` in this channel",
                )
            allowed_channels = {r.target_id for r in ch_rules if r.effect == "allow"}
            if allowed_channels and not (allowed_channels & subject.channel_ids):
                return deny(
                    "channel",
                    "This command can only be used in specific channels.",
                    f"Channel rule: `{key}` is limited to other channels",
                )
        trace.append("✓ No channel rule blocks this channel")

        # 6. Member rules (user > role > @everyone), most specific key first
        for key in keys:
            verdict = _member_verdict(rules, key, subject)
            if verdict is None:
                continue
            ok, step = verdict
            if ok:
                return allow(step)
            return deny("rule", "A server rule prevents you from using this command.", step)
        trace.append("✓ No user/role rule matched")

    # 7. Default access level
    have, need = LEVEL_NAMES[subject.level], LEVEL_NAMES[info.required]
    if subject.level >= info.required:
        return allow(f"Access level {have} meets the requirement ({need})")
    return deny(
        "level",
        f"This command requires **{need}** access or higher.",
        f"Access level {have} is below the requirement ({need})",
    )
