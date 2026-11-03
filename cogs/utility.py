"""Everyday utilities: info, calculator, reminders, to-dos, tags, AFK, bookmarks."""
import logging
import platform
import random
import time
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.access import LEVEL_NAMES
from core.helpers import format_duration, format_number, parse_duration, roll_dice, safe_calc
from core.security import Level, command_info, sec
from core.ui import clip, make_embed, reply

log = logging.getLogger(__name__)

MAX_REMINDERS = 25
MAX_TODOS = 50
MAX_TAGS = 200
EIGHT_BALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.", "Signs point to yes.",
    "Ask again later.", "Cannot predict now.", "Better not tell you now.",
    "Don't count on it.", "My sources say no.", "Very doubtful.", "Outlook not so good.",
]


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminder_loop.start()
        self.bookmark_menu = app_commands.ContextMenu(name="Bookmark Message", callback=self.bookmark)
        self.bookmark_menu.guild_only = True
        self.bot.tree.add_command(self.bookmark_menu)

    async def cog_unload(self):
        self.reminder_loop.cancel()
        self.bot.tree.remove_command(self.bookmark_menu.name, type=self.bookmark_menu.type)

    # ------------------------------------------------------------- general
    @app_commands.command(name="ping", description="Check the bot's latency")
    @sec(Level.EVERYONE)
    async def ping(self, interaction: discord.Interaction):
        await reply(interaction, content=f"🏓 Pong! `{round(self.bot.latency * 1000)} ms`")

    @app_commands.command(name="botinfo", description="Information about this bot")
    @sec(Level.EVERYONE)
    async def botinfo(self, interaction: discord.Interaction):
        embed = make_embed("Bot info", kind="info")
        embed.add_field(name="Servers", value=f"{len(self.bot.guilds):,}")
        embed.add_field(name="Uptime", value=format_duration(time.time() - self.bot.started_at))
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)} ms")
        embed.add_field(name="Python", value=platform.python_version())
        embed.add_field(name="discord.py", value=discord.__version__)
        await reply(interaction, embed=embed)

    @app_commands.command(name="help", description="Show the commands you are allowed to use")
    @sec(Level.EVERYONE)
    async def help(self, interaction: discord.Interaction):
        sm = self.bot.security
        grouped = {}
        for name, cmd in sorted(sm.command_objects().items()):
            if isinstance(cmd, app_commands.Group) or name == "help":
                continue
            info = command_info(cmd)
            decision = await sm.decide(interaction.user, cmd, interaction.channel)
            if decision.allowed:
                grouped.setdefault(info.cog or "other", []).append(f"`/{name}`")
        embed = make_embed("Commands you can use", "Only commands you can actually run are shown. Use `/security check` to see why one is missing.", "info")
        for cog, names in sorted(grouped.items()):
            embed.add_field(name=cog.replace("_", " ").title(), value=clip(" ".join(names), 1024), inline=False)
        await reply(interaction, embed=embed)

    # ---------------------------------------------------------------- info
    @app_commands.command(name="userinfo", description="Show information about a member")
    @app_commands.describe(member="Member (defaults to you)")
    @app_commands.guild_only()
    @sec(Level.EVERYONE)
    async def userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        embed = make_embed(str(member), kind="info")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id))
        embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
        level = await self.bot.security.member_level(member)
        embed.add_field(name="Bot access", value=LEVEL_NAMES[level])
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed.add_field(name=f"Roles ({len(roles)})", value=clip(" ".join(roles) or "None", 1024), inline=False)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="serverinfo", description="Show information about this server")
    @app_commands.guild_only()
    @sec(Level.EVERYONE)
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        embed = make_embed(g.name, kind="info")
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Owner", value=f"<@{g.owner_id}>")
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
        embed.add_field(name="Members", value=f"{g.member_count:,}")
        embed.add_field(name="Channels", value=f"{len(g.text_channels)} text / {len(g.voice_channels)} voice")
        embed.add_field(name="Roles", value=str(len(g.roles)))
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (tier {g.premium_tier})")
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="avatar", description="Show someone's avatar")
    @app_commands.describe(user="User (defaults to you)")
    @sec(Level.EVERYONE)
    async def avatar(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        user = user or interaction.user
        embed = make_embed(f"{user}'s avatar", kind="info")
        embed.set_image(url=user.display_avatar.with_size(1024).url)
        await reply(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="roleinfo", description="Show information about a role")
    @app_commands.describe(role="Role")
    @app_commands.guild_only()
    @sec(Level.EVERYONE)
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        embed = make_embed(role.name, kind="info")
        embed.add_field(name="ID", value=str(role.id))
        embed.add_field(name="Members", value=str(len(role.members)))
        embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
        embed.add_field(name="Color", value=str(role.color))
        embed.add_field(name="Mentionable", value="yes" if role.mentionable else "no")
        embed.add_field(name="Hoisted", value="yes" if role.hoist else "no")
        await reply(interaction, embed=embed)

    # --------------------------------------------------------------- tools
    @app_commands.command(name="calc", description="Calculate a math expression")
    @app_commands.describe(expression="e.g. 2*(3+4)^2 or sqrt(144)")
    @sec(Level.EVERYONE)
    async def calc(self, interaction: discord.Interaction, expression: app_commands.Range[str, 1, 200]):
        try:
            result = safe_calc(expression)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        await reply(interaction, embed=make_embed("Calculator", f"`{clip(expression, 150)}` = **{format_number(result)}**", "info"), ephemeral=False)

    @app_commands.command(name="timestamp", description="Build Discord timestamps from a relative time")
    @app_commands.describe(offset="e.g. 2h, 1d12h (from now)")
    @sec(Level.EVERYONE)
    async def timestamp(self, interaction: discord.Interaction, offset: str):
        try:
            delta = parse_duration(offset, min_seconds=1, max_seconds=5 * 365 * 86400)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        when = discord.utils.utcnow() + delta
        unix = int(when.timestamp())
        formats = "\n".join(f"`<t:{unix}:{f}>` -> <t:{unix}:{f}>" for f in ("t", "f", "F", "R"))
        await reply(interaction, embed=make_embed("Timestamps", formats, "info"))

    @app_commands.command(name="roll", description="Roll dice, e.g. d20 or 4d6+2")
    @app_commands.describe(dice="Dice notation")
    @sec(Level.EVERYONE)
    async def roll(self, interaction: discord.Interaction, dice: str = "d20"):
        try:
            rolls, mod, total = roll_dice(dice)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        shown = ", ".join(map(str, rolls[:30])) + (" ..." if len(rolls) > 30 else "")
        await reply(interaction, content=f"🎲 `{clip(dice, 20)}` -> [{shown}]{f' {mod:+d}' if mod else ''} = **{total}**", ephemeral=False)

    @app_commands.command(name="choose", description="Pick one option at random")
    @app_commands.describe(options="Options separated by commas")
    @sec(Level.EVERYONE)
    async def choose(self, interaction: discord.Interaction, options: app_commands.Range[str, 1, 500]):
        items = [o.strip() for o in options.split(",") if o.strip()]
        if len(items) < 2:
            return await reply(interaction, content="Give me at least two options separated by commas.")
        await reply(interaction, content=f"I choose: **{clip(random.SystemRandom().choice(items), 200)}**", ephemeral=False)

    @app_commands.command(name="coinflip", description="Flip a coin")
    @sec(Level.EVERYONE)
    async def coinflip(self, interaction: discord.Interaction):
        await reply(interaction, content=f"🪙 **{random.SystemRandom().choice(['Heads', 'Tails'])}**", ephemeral=False)

    @app_commands.command(name="8ball", description="Ask the magic 8-ball")
    @app_commands.describe(question="Your question")
    @sec(Level.EVERYONE)
    async def eightball(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200]):
        await reply(interaction, content=f"🎱 *{clip(question, 150)}*\n**{random.choice(EIGHT_BALL)}**", ephemeral=False)

    # ----------------------------------------------------------- reminders
    remind = app_commands.Group(name="remind", description="Personal reminders")

    @remind.command(name="set", description="Remind yourself about something")
    @app_commands.describe(when="e.g. 30m, 2h, 1d12h", message="What to remind you about", dm="Send it in DMs instead of this channel")
    @sec(Level.EVERYONE)
    async def remind_set(self, interaction: discord.Interaction, when: str, message: app_commands.Range[str, 1, 500], dm: bool = False):
        try:
            delta = parse_duration(when, min_seconds=10, max_seconds=365 * 86400)
        except ValueError as exc:
            return await reply(interaction, content=str(exc))
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM reminders WHERE user_id=$1", interaction.user.id)
        if count >= MAX_REMINDERS:
            return await reply(interaction, content=f"You already have {MAX_REMINDERS} reminders. Cancel some first.")
        due = discord.utils.utcnow() + delta
        rid = await self.bot.db.fetchval(
            "INSERT INTO reminders (user_id, guild_id, channel_id, message, remind_at) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            interaction.user.id, interaction.guild_id, None if dm or interaction.channel_id is None else interaction.channel_id, message, due,
        )
        await reply(interaction, embed=make_embed("Reminder set", f"I'll remind you {discord.utils.format_dt(due, 'R')}.\nID: `{rid}`", "ok"))

    @remind.command(name="list", description="Show your reminders")
    @sec(Level.EVERYONE)
    async def remind_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch("SELECT id, message, remind_at FROM reminders WHERE user_id=$1 ORDER BY remind_at LIMIT 25", interaction.user.id)
        if not rows:
            return await reply(interaction, content="You have no reminders.")
        lines = [f"`{r['id']}` {discord.utils.format_dt(r['remind_at'], 'R')} - {clip(r['message'], 80)}" for r in rows]
        await reply(interaction, embed=make_embed("Your reminders", "\n".join(lines), "info"))

    @remind.command(name="cancel", description="Cancel a reminder by its ID")
    @app_commands.describe(reminder_id="ID from /remind list")
    @sec(Level.EVERYONE)
    async def remind_cancel(self, interaction: discord.Interaction, reminder_id: app_commands.Range[int, 1, 2**62]):
        result = await self.bot.db.execute("DELETE FROM reminders WHERE id=$1 AND user_id=$2", reminder_id, interaction.user.id)
        await reply(interaction, content="Reminder cancelled." if not result.endswith(" 0") else "You have no reminder with that ID.")

    @tasks.loop(seconds=15)
    async def reminder_loop(self):
        try:
            rows = await self.bot.db.fetch(
                "DELETE FROM reminders WHERE id IN (SELECT id FROM reminders WHERE remind_at <= now() ORDER BY remind_at LIMIT 25 FOR UPDATE SKIP LOCKED) "
                "RETURNING user_id, channel_id, message, created_at"
            )
            for r in rows:
                embed = make_embed("⏰ Reminder", clip(r["message"], 1500), "info")
                embed.set_footer(text="You asked me to remind you")
                delivered = False
                if r["channel_id"]:
                    channel = self.bot.get_channel(r["channel_id"])
                    if channel is not None:
                        try:
                            await channel.send(content=f"<@{r['user_id']}>", embed=embed, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=r["user_id"])]))
                            delivered = True
                        except discord.HTTPException:
                            pass
                if not delivered:
                    try:
                        user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
                        await user.send(embed=embed)
                    except discord.HTTPException:
                        log.info("Could not deliver reminder to %s", r["user_id"])
        except Exception:
            log.exception("reminder loop failed")

    @reminder_loop.before_loop
    async def _before_reminders(self):
        await self.bot.wait_until_ready()

    # --------------------------------------------------------------- todos
    todo = app_commands.Group(name="todo", description="Your private to-do list")

    @todo.command(name="add", description="Add a to-do item")
    @app_commands.describe(task="What needs doing")
    @sec(Level.EVERYONE)
    async def todo_add(self, interaction: discord.Interaction, task: app_commands.Range[str, 1, 200]):
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM todos WHERE user_id=$1", interaction.user.id)
        if count >= MAX_TODOS:
            return await reply(interaction, content=f"Your list is full ({MAX_TODOS}). Remove some first.")
        tid = await self.bot.db.fetchval("INSERT INTO todos (user_id, content) VALUES ($1,$2) RETURNING id", interaction.user.id, task)
        await reply(interaction, content=f"Added `{tid}`: {task}")

    @todo.command(name="list", description="Show your to-do list")
    @sec(Level.EVERYONE)
    async def todo_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch("SELECT id, content, done FROM todos WHERE user_id=$1 ORDER BY done, id LIMIT 50", interaction.user.id)
        if not rows:
            return await reply(interaction, content="Your to-do list is empty.")
        lines = [f"{'✅' if r['done'] else '⬜'} `{r['id']}` {'~~' if r['done'] else ''}{clip(r['content'], 100)}{'~~' if r['done'] else ''}" for r in rows]
        await reply(interaction, embed=make_embed("Your to-do list", clip("\n".join(lines), 4000), "info"))

    @todo.command(name="done", description="Mark an item as done")
    @app_commands.describe(task_id="ID from /todo list")
    @sec(Level.EVERYONE)
    async def todo_done(self, interaction: discord.Interaction, task_id: app_commands.Range[int, 1, 2**62]):
        result = await self.bot.db.execute("UPDATE todos SET done = TRUE WHERE id=$1 AND user_id=$2", task_id, interaction.user.id)
        await reply(interaction, content="Marked as done." if not result.endswith(" 0") else "You have no item with that ID.")

    @todo.command(name="remove", description="Delete an item")
    @app_commands.describe(task_id="ID from /todo list")
    @sec(Level.EVERYONE)
    async def todo_remove(self, interaction: discord.Interaction, task_id: app_commands.Range[int, 1, 2**62]):
        result = await self.bot.db.execute("DELETE FROM todos WHERE id=$1 AND user_id=$2", task_id, interaction.user.id)
        await reply(interaction, content="Removed." if not result.endswith(" 0") else "You have no item with that ID.")

    @todo.command(name="clear", description="Delete all completed items")
    @sec(Level.EVERYONE)
    async def todo_clear(self, interaction: discord.Interaction):
        result = await self.bot.db.execute("DELETE FROM todos WHERE user_id=$1 AND done", interaction.user.id)
        await reply(interaction, content=f"Removed {result.split()[-1]} completed item(s).")

    # ---------------------------------------------------------------- tags
    tag = app_commands.Group(name="tag", description="Saved snippets for this server", guild_only=True)

    async def _tag_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetch("SELECT name FROM tags WHERE guild_id=$1 AND name LIKE $2 ORDER BY uses DESC LIMIT 25", interaction.guild_id, current.lower().replace("%", "").replace("_", r"\_") + "%")
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

    @tag.command(name="get", description="Show a saved tag")
    @app_commands.describe(name="Tag name")
    @app_commands.autocomplete(name=_tag_autocomplete)
    @sec(Level.EVERYONE)
    async def tag_get(self, interaction: discord.Interaction, name: str):
        row = await self.bot.db.fetchrow("UPDATE tags SET uses = uses + 1 WHERE guild_id=$1 AND name=$2 RETURNING content", interaction.guild.id, name.lower().strip())
        if row is None:
            return await reply(interaction, content="No tag with that name.")
        await reply(interaction, content=row["content"], ephemeral=False)

    @tag.command(name="create", description="Create a tag")
    @app_commands.describe(name="Short name (letters, numbers, - and _)", content="What the tag says")
    @sec(Level.TRUSTED)
    async def tag_create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 32], content: app_commands.Range[str, 1, 1800]):
        name = name.lower().strip()
        if not all(c.isalnum() or c in "-_" for c in name):
            return await reply(interaction, content="Tag names can only contain letters, numbers, - and _.")
        count = await self.bot.db.fetchval("SELECT COUNT(*) FROM tags WHERE guild_id=$1", interaction.guild.id)
        if count >= MAX_TAGS:
            return await reply(interaction, content="This server reached the tag limit.")
        result = await self.bot.db.execute("INSERT INTO tags (guild_id, name, content, author_id) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING", interaction.guild.id, name, content, interaction.user.id)
        if result.endswith(" 0"):
            return await reply(interaction, content="A tag with that name already exists.")
        await reply(interaction, content=f"Tag `{name}` created.")

    @tag.command(name="delete", description="Delete a tag (author or staff)")
    @app_commands.describe(name="Tag name")
    @app_commands.autocomplete(name=_tag_autocomplete)
    @sec(Level.TRUSTED)
    async def tag_delete(self, interaction: discord.Interaction, name: str):
        name = name.lower().strip()
        row = await self.bot.db.fetchrow("SELECT author_id FROM tags WHERE guild_id=$1 AND name=$2", interaction.guild.id, name)
        if row is None:
            return await reply(interaction, content="No tag with that name.")
        if row["author_id"] != interaction.user.id and await self.bot.security.member_level(interaction.user) < Level.MOD:
            return await reply(interaction, content="Only the author or a moderator can delete this tag.")
        await self.bot.db.execute("DELETE FROM tags WHERE guild_id=$1 AND name=$2", interaction.guild.id, name)
        await reply(interaction, content=f"Tag `{name}` deleted.")

    @tag.command(name="list", description="List this server's tags")
    @sec(Level.EVERYONE)
    async def tag_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetch("SELECT name FROM tags WHERE guild_id=$1 ORDER BY name LIMIT 100", interaction.guild.id)
        if not rows:
            return await reply(interaction, content="No tags yet.")
        await reply(interaction, embed=make_embed("Tags", clip(", ".join(f"`{r['name']}`" for r in rows), 4000), "info"))

    # ----------------------------------------------------------------- afk
    @app_commands.command(name="afk", description="Set yourself as away; people who mention you will be told")
    @app_commands.describe(reason="Why you're away")
    @app_commands.guild_only()
    @sec(Level.EVERYONE)
    async def afk(self, interaction: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 100]] = None):
        await self.bot.db.execute(
            "INSERT INTO afk (guild_id, user_id, reason) VALUES ($1,$2,$3) ON CONFLICT (guild_id, user_id) DO UPDATE SET reason=EXCLUDED.reason, since=now()",
            interaction.guild.id, interaction.user.id, reason,
        )
        self.bot.dispatch("afk_changed", interaction.guild.id, interaction.user.id, True)
        await reply(interaction, content=f"You're now AFK{': ' + reason if reason else ''}. I'll clear it when you next talk.")

    # ------------------------------------------------------- context menu
    @app_commands.guild_only()
    @sec(Level.EVERYONE)
    async def bookmark(self, interaction: discord.Interaction, message: discord.Message):
        embed = make_embed("🔖 Bookmarked message", clip(message.content or "*(no text)*", 1800), "info")
        embed.add_field(name="Author", value=str(message.author))
        embed.add_field(name="Link", value=f"[Jump to message]({message.jump_url})")
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)
        try:
            await interaction.user.send(embed=embed)
        except discord.HTTPException:
            return await reply(interaction, content="I couldn't DM you. Enable DMs from server members.")
        await reply(interaction, content="Bookmark sent to your DMs.")


# The decorator order above applies @sec to the plain function; the context
# menu is constructed in __init__ from the bound method, which keeps __sec_level__.


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
