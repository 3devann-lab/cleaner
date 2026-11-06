"""Polls and giveaways. Buttons are persistent (they keep working after a restart)."""
import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.helpers import parse_duration
from core.security import Level, sec
from core.ui import clip, make_embed, mentions, reply

log = logging.getLogger(__name__)

NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
BAR = 12


# ------------------------------------------------------------------- polls
def poll_embed(question, options, counts, closed, ends_at) -> discord.Embed:
    total = sum(counts)
    lines = []
    for i, option in enumerate(options):
        count = counts[i]
        pct = (count / total * 100) if total else 0
        filled = round(pct / 100 * BAR)
        lines.append(f"{NUMS[i]} **{clip(option, 100)}**\n`{'█' * filled}{'░' * (BAR - filled)}` {count} ({pct:.0f}%)")
    embed = make_embed(f"📊 {clip(question, 250)}", "\n\n".join(lines), "warn" if closed else "info")
    if closed:
        embed.set_footer(text=f"Poll closed - {total} vote(s)")
    elif ends_at:
        embed.add_field(name="Ends", value=discord.utils.format_dt(ends_at, "R"), inline=False)
        embed.set_footer(text=f"{total} vote(s) - press a button again to remove your vote")
    else:
        embed.set_footer(text=f"{total} vote(s) - press a button again to remove your vote")
    return embed


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<poll>[0-9]+):(?P<idx>[0-9]+)"):
    def __init__(self, poll_id: int, idx: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=str(idx + 1),
                custom_id=f"poll:{poll_id}:{idx}",
                disabled=disabled,
                row=idx // 5,
            )
        )
        self.poll_id, self.idx = poll_id, idx

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["poll"]), int(match["idx"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction.client.security.check_component(interaction)

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Community").handle_vote(interaction, self.poll_id, self.idx)


def poll_view(poll_id: int, n: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i in range(n):
        view.add_item(PollButton(poll_id, i, disabled))
    return view


# --------------------------------------------------------------- giveaways
class GiveawayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gw:(?P<id>[0-9]+)"):
    def __init__(self, giveaway_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(style=discord.ButtonStyle.success, label="🎉 Enter", custom_id=f"gw:{giveaway_id}", disabled=disabled)
        )
        self.giveaway_id = giveaway_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction.client.security.check_component(interaction)

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Community").handle_enter(interaction, self.giveaway_id)


def giveaway_embed(row, entries: int = 0, winners_text: Optional[str] = None) -> discord.Embed:
    ended = row["ended"]
    embed = make_embed(f"🎁 {clip(row['prize'], 200)}", kind="warn" if ended else "ok")
    embed.add_field(name="Hosted by", value=f"<@{row['host_id']}>")
    embed.add_field(name="Winners", value=str(row["winners"]))
    embed.add_field(name="Ended" if ended else "Ends", value=discord.utils.format_dt(row["ends_at"], "R"))
    if row["required_role_id"]:
        embed.add_field(name="Requires role", value=f"<@&{row['required_role_id']}>")
    if winners_text is not None:
        embed.add_field(name="Winner(s)", value=winners_text, inline=False)
    embed.set_footer(text=f"Giveaway #{row['id']}")
    return embed


class Community(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_dynamic_items(PollButton, GiveawayButton)
        self.expiry_loop.start()

    async def cog_unload(self):
        self.expiry_loop.cancel()
        self.bot.remove_dynamic_items(PollButton, GiveawayButton)

    # ----------------------------------------------------------------- poll
    poll = app_commands.Group(name="poll", description="Polls with live results", guild_only=True)

    @poll.command(name="create", description="Create a poll")
    @app_commands.describe(question="The question", options="2-10 options separated by |", duration="Optional end time, e.g. 1h or 2d")
    @sec(Level.TRUSTED)
    async def poll_create(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 250], options: str, duration: Optional[str] = None):
        items = [o.strip() for o in options.split("|") if o.strip()]
        if not 2 <= len(items) <= 10:
            return await reply(interaction, content="Give 2 to 10 options separated by `|`.")
        if any(len(o) > 100 for o in items):
            return await reply(interaction, content="Each option can be at most 100 characters.")
        ends_at = None
        if duration:
            try:
                ends_at = discord.utils.utcnow() + parse_duration(duration, min_seconds=60, max_seconds=30 * 86400)
            except ValueError as exc:
                return await reply(interaction, content=str(exc))
        poll_id = await self.bot.db.fetchval(
            "INSERT INTO polls (guild_id, channel_id, question, options, creator_id, ends_at) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            interaction.guild.id, interaction.channel_id, question, items, interaction.user.id, ends_at,
        )
        try:
            await interaction.response.send_message(embed=poll_embed(question, items, [0] * len(items), False, ends_at), view=poll_view(poll_id, len(items)))
            message = await interaction.original_response()
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM polls WHERE id=$1", poll_id)
            raise
        await self.bot.db.execute("UPDATE polls SET message_id=$2 WHERE id=$1", poll_id, message.id)

    async def _counts(self, poll_id: int, n: int):
        rows = await self.bot.db.fetch("SELECT option_idx, COUNT(*) AS c FROM poll_votes WHERE poll_id=$1 GROUP BY option_idx", poll_id)
        counts = [0] * n
        for r in rows:
            if r["option_idx"] < n:
                counts[r["option_idx"]] = r["c"]
        return counts

    async def handle_vote(self, interaction: discord.Interaction, poll_id: int, idx: int):
        poll = await self.bot.db.fetchrow("SELECT * FROM polls WHERE id=$1 AND guild_id=$2", poll_id, interaction.guild_id)
        if poll is None or poll["closed"]:
            return await interaction.response.send_message("This poll is closed.", ephemeral=True)
        if idx >= len(poll["options"]):
            return await interaction.response.send_message("Invalid option.", ephemeral=True)
        existing = await self.bot.db.fetchval("SELECT option_idx FROM poll_votes WHERE poll_id=$1 AND user_id=$2", poll_id, interaction.user.id)
        if existing == idx:
            await self.bot.db.execute("DELETE FROM poll_votes WHERE poll_id=$1 AND user_id=$2", poll_id, interaction.user.id)
        else:
            await self.bot.db.execute(
                "INSERT INTO poll_votes (poll_id, user_id, option_idx) VALUES ($1,$2,$3) "
                "ON CONFLICT (poll_id, user_id) DO UPDATE SET option_idx = EXCLUDED.option_idx",
                poll_id, interaction.user.id, idx,
            )
        counts = await self._counts(poll_id, len(poll["options"]))
        await interaction.response.edit_message(embed=poll_embed(poll["question"], list(poll["options"]), counts, False, poll["ends_at"]))

    @poll.command(name="end", description="Close a poll now")
    @app_commands.describe(poll_id="Poll number (shown in /poll list)")
    @sec(Level.TRUSTED)
    async def poll_end(self, interaction: discord.Interaction, poll_id: app_commands.Range[int, 1, 2**53 - 1]):
        poll = await self.bot.db.fetchrow("SELECT * FROM polls WHERE id=$1 AND guild_id=$2 AND NOT closed", poll_id, interaction.guild.id)
        if poll is None:
            return await reply(interaction, content="No open poll with that ID.")
        if poll["creator_id"] != interaction.user.id and await self.bot.security.member_level(interaction.user) < Level.MOD:
            return await reply(interaction, content="Only the creator or a moderator can end this poll.")
        await self._close_poll(poll)
        await reply(interaction, content="Poll closed.")

    @poll.command(name="list", description="Open polls in this server")
    @sec(Level.EVERYONE)
    async def poll_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch("SELECT id, question, channel_id FROM polls WHERE guild_id=$1 AND NOT closed ORDER BY id DESC LIMIT 20", interaction.guild.id)
        if not rows:
            return await reply(interaction, content="No open polls.")
        await reply(interaction, embed=make_embed("Open polls", "\n".join(f"`{r['id']}` <#{r['channel_id']}> - {clip(r['question'], 80)}" for r in rows), "info"))

    async def _close_poll(self, poll):
        await self.bot.db.execute("UPDATE polls SET closed = TRUE WHERE id=$1", poll["id"])
        if not poll["message_id"]:
            return
        channel = self.bot.get_channel(poll["channel_id"])
        if channel is None:
            return
        try:
            counts = await self._counts(poll["id"], len(poll["options"]))
            message = channel.get_partial_message(poll["message_id"])
            await message.edit(embed=poll_embed(poll["question"], list(poll["options"]), counts, True, None), view=poll_view(poll["id"], len(poll["options"]), True))
        except discord.HTTPException:
            log.info("Could not update closed poll %s", poll["id"])

    # ------------------------------------------------------------ giveaways
    giveaway = app_commands.Group(name="giveaway", description="Run giveaways", guild_only=True)

    @giveaway.command(name="start", description="Start a giveaway")
    @app_commands.describe(prize="What is being given away", duration="e.g. 1h, 2d", winners="Number of winners", required_role="Only members with this role can enter")
    @sec(Level.MOD)
    async def giveaway_start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200], duration: str, winners: app_commands.Range[int, 1, 20] = 1, required_role: Optional[discord.Role] = None):
        try:
            delta = parse_duration(duration, min_seconds=60, max_seconds=60 * 86400)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        ends_at = discord.utils.utcnow() + delta
        gid = await self.bot.db.fetchval(
            "INSERT INTO giveaways (guild_id, channel_id, prize, winners, host_id, required_role_id, ends_at) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            interaction.guild.id, interaction.channel_id, prize, winners, interaction.user.id, required_role.id if required_role else None, ends_at,
        )
        row = await self.bot.db.fetchrow("SELECT * FROM giveaways WHERE id=$1", gid)
        view = discord.ui.View(timeout=None)
        view.add_item(GiveawayButton(gid))
        try:
            await interaction.response.send_message(embed=giveaway_embed(row), view=view)
            message = await interaction.original_response()
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM giveaways WHERE id=$1", gid)
            raise
        await self.bot.db.execute("UPDATE giveaways SET message_id=$2 WHERE id=$1", gid, message.id)

    async def handle_enter(self, interaction: discord.Interaction, giveaway_id: int):
        row = await self.bot.db.fetchrow("SELECT * FROM giveaways WHERE id=$1 AND guild_id=$2", giveaway_id, interaction.guild_id)
        if row is None or row["ended"]:
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
        if row["required_role_id"] and not any(r.id == row["required_role_id"] for r in interaction.user.roles):
            return await interaction.response.send_message(f"You need <@&{row['required_role_id']}> to enter.", ephemeral=True)
        result = await self.bot.db.execute("INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING", giveaway_id, interaction.user.id)
        if result.endswith(" 0"):
            await self.bot.db.execute("DELETE FROM giveaway_entries WHERE giveaway_id=$1 AND user_id=$2", giveaway_id, interaction.user.id)
            return await interaction.response.send_message("You left the giveaway.", ephemeral=True)
        await interaction.response.send_message("🎉 You're in! Press the button again to leave.", ephemeral=True)

    async def _pick_winners(self, guild: discord.Guild, row, exclude=()):
        rows = await self.bot.db.fetch("SELECT user_id FROM giveaway_entries WHERE giveaway_id=$1", row["id"])
        pool = []
        for r in rows:
            uid = r["user_id"]
            member = guild.get_member(uid)
            if member is None or member.bot or uid in exclude:
                continue
            if row["required_role_id"] and not any(role.id == row["required_role_id"] for role in member.roles):
                continue
            pool.append(uid)
        return random.SystemRandom().sample(pool, min(row["winners"], len(pool)))

    async def _end_giveaway(self, row, announce_prefix="🎉"):
        await self.bot.db.execute("UPDATE giveaways SET ended = TRUE WHERE id=$1", row["id"])
        guild = self.bot.get_guild(row["guild_id"])
        channel = self.bot.get_channel(row["channel_id"])
        if guild is None or channel is None:
            return []
        winners = await self._pick_winners(guild, row)
        text = ", ".join(f"<@{w}>" for w in winners) or "No valid entries"
        ended_row = dict(row, ended=True)
        try:
            if row["message_id"]:
                view = discord.ui.View(timeout=None)
                view.add_item(GiveawayButton(row["id"], disabled=True))
                await channel.get_partial_message(row["message_id"]).edit(embed=giveaway_embed(ended_row, winners_text=text), view=view)
            if winners:
                await channel.send(f"{announce_prefix} Congratulations {text}! You won **{clip(row['prize'], 150)}**.", allowed_mentions=mentions(winners))
            else:
                await channel.send(f"No valid entries for **{clip(row['prize'], 150)}**.")
        except discord.HTTPException:
            log.info("Could not announce giveaway %s", row["id"])
        return winners

    @giveaway.command(name="end", description="End a giveaway now")
    @app_commands.describe(giveaway_id="Giveaway number")
    @sec(Level.MOD)
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: app_commands.Range[int, 1, 2**53 - 1]):
        row = await self.bot.db.fetchrow("SELECT * FROM giveaways WHERE id=$1 AND guild_id=$2 AND NOT ended", giveaway_id, interaction.guild.id)
        if row is None:
            return await reply(interaction, content="No running giveaway with that ID.")
        await interaction.response.defer(ephemeral=True)
        await self._end_giveaway(row)
        await reply(interaction, content="Giveaway ended.")

    @giveaway.command(name="reroll", description="Pick new winner(s) for an ended giveaway")
    @app_commands.describe(giveaway_id="Giveaway number")
    @sec(Level.MOD)
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway_id: app_commands.Range[int, 1, 2**53 - 1]):
        row = await self.bot.db.fetchrow("SELECT * FROM giveaways WHERE id=$1 AND guild_id=$2 AND ended", giveaway_id, interaction.guild.id)
        if row is None:
            return await reply(interaction, content="No ended giveaway with that ID.")
        winners = await self._pick_winners(interaction.guild, row)
        if not winners:
            return await reply(interaction, content="No valid entries to pick from.")
        await interaction.response.send_message(f"🎉 New winner(s) for **{clip(row['prize'], 150)}**: " + ", ".join(f"<@{w}>" for w in winners), allowed_mentions=mentions(winners))

    @giveaway.command(name="list", description="Running giveaways")
    @sec(Level.EVERYONE)
    async def giveaway_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch("SELECT id, prize, ends_at, channel_id FROM giveaways WHERE guild_id=$1 AND NOT ended ORDER BY ends_at LIMIT 20", interaction.guild.id)
        if not rows:
            return await reply(interaction, content="No running giveaways.")
        await reply(interaction, embed=make_embed("Running giveaways", "\n".join(f"`{r['id']}` <#{r['channel_id']}> - {clip(r['prize'], 60)} (ends {discord.utils.format_dt(r['ends_at'], 'R')})" for r in rows), "info"))

    # ----------------------------------------------------------------- loop
    @tasks.loop(seconds=20)
    async def expiry_loop(self):
        try:
            for poll in await self.bot.db.fetch("SELECT * FROM polls WHERE NOT closed AND ends_at IS NOT NULL AND ends_at <= now() LIMIT 20"):
                await self._close_poll(poll)
            for row in await self.bot.db.fetch("SELECT * FROM giveaways WHERE NOT ended AND ends_at <= now() LIMIT 20"):
                await self._end_giveaway(row)
        except Exception:
            log.exception("community expiry loop failed")

    @expiry_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Community(bot))
