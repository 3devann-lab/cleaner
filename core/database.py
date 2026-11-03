"""PostgreSQL access (Railway Postgres) via asyncpg.

All queries in this project use parameter placeholders ($1, $2, ...) - never
string formatting - so user input can never alter the SQL.
"""
import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id             BIGINT PRIMARY KEY,
    log_channel_id       BIGINT,
    lockdown             BOOLEAN NOT NULL DEFAULT FALSE,
    case_counter         INTEGER NOT NULL DEFAULT 0,
    welcome_channel_id   BIGINT,
    welcome_message      TEXT,
    autorole_id          BIGINT,
    antiraid_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    raid_threshold       INTEGER NOT NULL DEFAULT 8,
    raid_window          INTEGER NOT NULL DEFAULT 20,
    raid_action          TEXT NOT NULL DEFAULT 'alert',
    min_account_age_days INTEGER NOT NULL DEFAULT 0,
    ticket_category_id   BIGINT,
    ticket_staff_role_id BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS guild_roles (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    level    SMALLINT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS command_rules (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    command     TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('user', 'role', 'channel')),
    target_id   BIGINT NOT NULL,
    effect      TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    created_by  BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    UNIQUE (guild_id, command, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_rules_guild ON command_rules (guild_id);

-- guild_id = 0 means a GLOBAL blacklist entry (bot owner only)
CREATE TABLE IF NOT EXISTS blacklist (
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    reason     TEXT,
    added_by   BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    actor_id   BIGINT,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log (guild_id, id DESC);

CREATE TABLE IF NOT EXISTS mod_cases (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    case_number  INTEGER NOT NULL,
    user_id      BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    action       TEXT NOT NULL,
    reason       TEXT,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    UNIQUE (guild_id, case_number)
);
CREATE INDEX IF NOT EXISTS idx_cases_user ON mod_cases (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_cases_expiry ON mod_cases (expires_at) WHERE active;

CREATE TABLE IF NOT EXISTS reminders (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    guild_id   BIGINT,
    channel_id BIGINT,
    message    TEXT NOT NULL,
    remind_at  TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (remind_at);

CREATE TABLE IF NOT EXISTS todos (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    content    TEXT NOT NULL,
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_todos_user ON todos (user_id);

CREATE TABLE IF NOT EXISTS tags (
    guild_id   BIGINT NOT NULL,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    author_id  BIGINT NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS afk (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    reason   TEXT,
    since    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS polls (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,
    question   TEXT NOT NULL,
    options    TEXT[] NOT NULL,
    creator_id BIGINT NOT NULL,
    ends_at    TIMESTAMPTZ,
    closed     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id    BIGINT NOT NULL REFERENCES polls (id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    option_idx SMALLINT NOT NULL,
    PRIMARY KEY (poll_id, user_id)
);

CREATE TABLE IF NOT EXISTS giveaways (
    id               BIGSERIAL PRIMARY KEY,
    guild_id         BIGINT NOT NULL,
    channel_id       BIGINT NOT NULL,
    message_id       BIGINT,
    prize            TEXT NOT NULL,
    winners          SMALLINT NOT NULL DEFAULT 1,
    host_id          BIGINT NOT NULL,
    required_role_id BIGINT,
    ends_at          TIMESTAMPTZ NOT NULL,
    ended            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id BIGINT NOT NULL REFERENCES giveaways (id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT UNIQUE,
    user_id    BIGINT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (guild_id, user_id, status);
"""


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self) -> None:
        last_error = None
        for attempt in range(1, 7):
            try:
                self.pool = await asyncpg.create_pool(
                    self.dsn, min_size=1, max_size=8, command_timeout=30
                )
                break
            except (OSError, asyncpg.PostgresError) as exc:
                last_error = exc
                wait = min(2 ** attempt, 20)
                log.warning("Database not ready (attempt %s/6): %s. Retrying in %ss", attempt, exc, wait)
                await asyncio.sleep(wait)
        else:
            raise RuntimeError("Could not connect to the database") from last_error

        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        log.info("Database connected and schema verified")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    # Thin pass-throughs so cogs can write `await self.bot.db.fetch(...)`.
    async def execute(self, query: str, *args):
        return await self.pool.execute(query, *args)

    async def fetch(self, query: str, *args):
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        return await self.pool.fetchval(query, *args)
