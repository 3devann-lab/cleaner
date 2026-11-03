"""Bot entrypoint. Railway auto-detects `main.py` and runs it with `python main.py`."""
import asyncio
import logging
import time

import discord
from discord.ext import commands

import config
from core.database import Database
from core.security import SecureTree, SecurityManager

log = logging.getLogger("bot")

EXTENSIONS = (
    "cogs.security_admin",
    "cogs.moderation",
    "cogs.utility",
    "cogs.community",
    "cogs.events",
    "cogs.tickets",
    "cogs.owner",
)


class UtilityBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # privileged: enable "Server Members Intent" in the dev portal
        # NOTE: message_content is intentionally NOT requested (least privilege).
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=SecureTree,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),  # the bot can never mass-ping
            activity=discord.Activity(type=discord.ActivityType.watching, name="/help"),
        )
        self.db = Database(config.DATABASE_URL)
        self.security = SecurityManager(self)
        self.started_at = time.time()
        self.dashboard = None  # aiohttp runner, started in setup_hook when DASH_PASSWORD is set

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.security.load_global()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded %s", extension)

        if config.dashboard_enabled():
            from dashboard.server import start_dashboard

            self.dashboard = await start_dashboard(self)

        if config.SYNC_ON_START:
            if config.DEV_GUILD_ID:
                guild = discord.Object(id=config.DEV_GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %s commands to dev guild %s", len(synced), config.DEV_GUILD_ID)
            else:
                synced = await self.tree.sync()
                log.info("Synced %s global commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %s servers", self.user, self.user.id, len(self.guilds))

    async def close(self) -> None:
        if self.dashboard is not None:
            from dashboard.server import stop_dashboard

            await stop_dashboard(self.dashboard)
        await super().close()
        await self.db.close()


async def main() -> None:
    config.validate()
    discord.utils.setup_logging(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
    bot = UtilityBot()
    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
