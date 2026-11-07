"""Discord-facing operations for the dashboard: read the layout, apply a plan, change the icon."""
from __future__ import annotations

import logging

import discord

from dashboard import layout

log = logging.getLogger(__name__)

REASON = "Dashboard organizer"

_TYPE_NAMES = {
    discord.ChannelType.text: "text",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.news: "announcement",
}
if hasattr(discord.ChannelType, "media"):
    _TYPE_NAMES[discord.ChannelType.media] = "media"


def type_name(channel) -> str:
    return _TYPE_NAMES.get(channel.type, "other")


def _sort_key(ch):
    return (layout.group_of(type_name(ch)), ch.position, ch.id)


def snapshot(guild: discord.Guild) -> dict:
    def conv(ch):
        return {"id": str(ch.id), "name": ch.name, "type": type_name(ch)}

    top = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel) and getattr(c, "category_id", None) is None]
    cats = sorted(guild.categories, key=lambda c: (c.position, c.id))
    return {
        "uncategorized": [conv(c) for c in sorted(top, key=_sort_key)],
        "categories": [
            {"id": str(cat.id), "name": cat.name, "channels": [conv(c) for c in sorted(cat.channels, key=_sort_key)]}
            for cat in cats
        ],
    }


def guild_info(guild: discord.Guild) -> dict:
    perms = guild.me.guild_permissions if guild.me else discord.Permissions.none()
    return {
        "id": str(guild.id),
        "name": guild.name,
        "icon_url": guild.icon.with_size(256).url if guild.icon else None,
        "member_count": guild.member_count,
        "can_manage_channels": perms.manage_channels or perms.administrator,
        "can_manage_guild": perms.manage_guild or perms.administrator,
    }


async def apply_layout(bot, guild: discord.Guild, snap: dict, clean: dict, *, sync_permissions: bool, progress) -> list:
    """Apply a validated draft. Every step is attempted independently; failures are reported, not fatal."""
    results = []

    def note(ok: bool, text: str, error: str = None):
        entry = {"ok": ok, "text": text, "error": error}
        results.append(entry)
        progress(entry)

    cats_idx, chans_idx = layout.index_snapshot(snap)
    id_map = {}
    created_cats = {}

    # 1. deletions: channels first, then categories
    for d in sorted(clean["deleted"], key=lambda x: x["type"] == "category"):
        channel = guild.get_channel(int(d["id"]))
        label = f'Delete {d["type"]} "{d["name"]}"'
        if channel is None:
            note(False, label, "It no longer exists.")
            continue
        try:
            await channel.delete(reason=REASON)
            note(True, label)
        except discord.HTTPException as exc:
            note(False, label, _err(exc))

    # 2. new categories
    for cat in clean["categories"]:
        if cat["id"] is None:
            label = f'Create category "{cat["name"]}"'
            try:
                made = await guild.create_category(cat["name"], reason=REASON)
                id_map[cat["tmp"]] = str(made.id)
                created_cats[cat["tmp"]] = made
                note(True, label)
            except discord.HTTPException as exc:
                note(False, label, _err(exc))

    # 3. renames
    def rename_targets():
        for cat in clean["categories"]:
            if cat["id"] and cat["name"] != cats_idx[cat["id"]]["name"]:
                yield cat["id"], cats_idx[cat["id"]]["name"], cat["name"], "category"
            for ch in cat["channels"]:
                yield from _chan_rename(ch)
        for ch in clean["uncategorized"]:
            yield from _chan_rename(ch)

    def _chan_rename(ch):
        if ch["id"] and ch["name"] != chans_idx[ch["id"]][0]["name"]:
            yield ch["id"], chans_idx[ch["id"]][0]["name"], ch["name"], ch["type"]

    for cid, old, new, kind in list(rename_targets()):
        label = f'Rename {kind} "{old}" → "{new}"'
        channel = guild.get_channel(int(cid))
        if channel is None:
            note(False, label, "It no longer exists.")
            continue
        try:
            await channel.edit(name=new, reason=REASON)
            note(True, label)
        except discord.HTTPException as exc:
            note(False, label, _err(exc))

    # 4. new channels
    def create_targets():
        for cat in clean["categories"]:
            for ch in cat["channels"]:
                if ch["id"] is None:
                    yield ch, cat
        for ch in clean["uncategorized"]:
            if ch["id"] is None:
                yield ch, None

    for ch, cat in list(create_targets()):
        label = f'Create {ch["type"]} channel "{ch["name"]}"'
        parent = None
        if cat is not None:
            parent = created_cats.get(cat["tmp"]) if cat["id"] is None else guild.get_channel(int(cat["id"]))
            if parent is None:
                label += " (its category is unavailable, so it goes at the top level)"
        try:
            if ch["type"] == "voice":
                made = await guild.create_voice_channel(ch["name"], category=parent, reason=REASON)
            else:
                made = await guild.create_text_channel(ch["name"], category=parent, topic=ch["topic"], reason=REASON)
            id_map[ch["tmp"]] = str(made.id)
            note(True, label)
        except discord.HTTPException as exc:
            note(False, label, _err(exc))

    # 5a. category membership. Discord allows only ONE channel's parent_id to change
    #     per bulk request, so existing channels are moved one at a time.
    moved = []
    for cat in clean["categories"]:
        for ch in cat["channels"]:
            if ch["id"] and chans_idx[ch["id"]][1] != (cat["id"] or id_map.get(cat["tmp"])):
                moved.append(ch)
    for ch in clean["uncategorized"]:
        if ch["id"] and chans_idx[ch["id"]][1] is not None:
            moved.append(ch)
    payload = layout.plan_positions(clean, id_map)
    target_parent = {str(p["id"]): p.get("parent_id") for p in payload}
    created_by_id = {int(id_map[t]): obj for t, obj in created_cats.items() if t in id_map}
    for ch in moved:
        label = f'Move "{ch["name"]}" to its category'
        channel = guild.get_channel(int(ch["id"]))
        if channel is None:
            note(False, label, "It no longer exists.")
            continue
        parent_id = target_parent.get(ch["id"])
        parent = created_by_id.get(parent_id) or (guild.get_channel(parent_id) if parent_id else None)
        if parent_id and parent is None:
            note(False, label, "The target category is unavailable.")
            continue
        try:
            await channel.edit(
                category=parent,
                sync_permissions=bool(sync_permissions and parent is not None),
                reason=REASON,
            )
            note(True, label)
        except discord.HTTPException as exc:
            note(False, label, _err(exc))

    # 5b. order only: bulk updates WITHOUT parent_id, one request per parent
    groups = {}
    for p in payload:
        groups.setdefault(p.get("parent_id"), []).append({"id": p["id"], "position": p["position"]})
    done = 0
    for entries in groups.values():
        try:
            await bot.http.bulk_channel_update(guild.id, entries, reason=REASON)
            done += len(entries)
        except discord.HTTPException as exc:
            note(False, "Set the order", _err(exc))
    if done:
        note(True, f"Set the order of {done} items")
    return results


def _err(exc: discord.HTTPException) -> str:
    if isinstance(exc, discord.Forbidden):
        return "Missing permission. Give the bot Manage Channels and put its role above the affected roles."
    return (getattr(exc, "text", "") or str(exc))[:200]


async def set_icon(guild: discord.Guild, data):
    """data = image bytes, or None to remove the icon."""
    await guild.edit(icon=data, reason="Dashboard: server icon changed")
