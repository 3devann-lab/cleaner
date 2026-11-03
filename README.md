# Utility Discord Bot + Web Dashboard

A Python Discord bot with a permission engine, moderation, utilities, community tools, tickets, anti-raid, and a
password-protected web dashboard (channel editor, server icon, and a Groq AI auto-organizer with a review step).
Runs on Railway with Railway Postgres.

## 1. GitHub layout

Put the files in your repo **exactly like this** (`main.py` must be in the repo root so Railway finds it):

```
your-repo/
├── main.py
├── config.py
├── requirements.txt
├── .python-version
├── .gitignore
├── .env.example
├── README.md
├── core/
│   ├── __init__.py
│   ├── access.py
│   ├── database.py
│   ├── helpers.py
│   ├── security.py
│   └── ui.py
├── cogs/
│   ├── __init__.py
│   ├── security_admin.py
│   ├── moderation.py
│   ├── utility.py
│   ├── community.py
│   ├── events.py
│   ├── tickets.py
│   └── owner.py
├── dashboard/
│   ├── __init__.py
│   ├── server.py
│   ├── layout.py
│   ├── groq_ai.py
│   ├── guild_ops.py
│   └── static/
│       ├── app.html
│       ├── app.js
│       ├── login.html
│       └── style.css
└── tests/            (optional, not needed to run the bot)
    ├── test_access.py
    ├── test_helpers.py
    └── test_layout.py
```

Never commit a real `.env`. It is git-ignored; all secrets live in Railway's Variables tab.

## 2. Discord Developer Portal

1. https://discord.com/developers/applications → **New Application** → **Bot** → copy the **token** (this is `DISCORD_TOKEN`).
2. On the Bot page turn on **Server Members Intent**. (Message Content Intent is *not* needed.)
3. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`. Bot permissions: View Channels, Send Messages,
   Embed Links, Read Message History, Manage Channels, Manage Server, Manage Roles, Manage Messages, Manage Nicknames,
   Kick Members, Ban Members, Moderate Members. Open the generated URL to invite the bot.
4. In your server, drag the bot's role **above** the roles it should moderate or assign.
5. Your own user ID (Settings → Advanced → Developer Mode, then right-click yourself → Copy User ID) goes in `OWNER_IDS`.

## 3. Railway

1. **New Project → Deploy from GitHub repo** → pick your repo.
2. In the same project: **+ New → Database → Add PostgreSQL**.
3. Open your **bot service → Variables** and add:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | your bot token |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference to the Postgres service; rename `Postgres` if yours is named differently) |
| `OWNER_IDS` | your Discord user ID (comma-separate several) |
| `DASH_PASSWORD` | **the dashboard password**, 10+ characters (the dashboard stays off if this is missing or too short) |
| `GROQ_API_KEY` | from https://console.groq.com/keys (only needed for the AI organizer) |
| `GROQ_MODEL` | optional, default `llama-3.3-70b-versatile` |
| `DASH_GUILD_IDS` | optional, restrict the dashboard to these server IDs |

4. **Settings → Networking → Generate Domain**. That URL is your dashboard (`https://<your-domain>/`). Railway sets `PORT` for you.
5. If Railway says it can't find a start command, set **Settings → Deploy → Custom Start Command** to `python main.py`.
6. Tables are created automatically on first start. Slash commands sync on startup (global commands can take a few minutes to appear).

## 4. How command security works

Every slash command and context menu passes through one gate **before its code runs**. Checks, in order:

1. Global blacklist (bot owner), then server blacklist → denied
2. Bot owner / server owner → always allowed (can't be locked out)
3. Server **lockdown** (only staff can use commands)
4. **Channel rules** (disable a command in a channel/category, or limit it to certain channels)
5. **User > role > @everyone rules**, most specific command first: `mod ban` → `mod` → `cog:moderation` → `*`. Deny beats allow within a tier. Rules can expire.
6. Default access level of the command: Everyone < Trusted < Moderator < Administrator < Server owner < Bot owner

Staff level comes from Discord permissions (Manage Server/Admin → Administrator; Kick/Ban/Manage Messages/Timeout → Moderator) plus roles you map with `/security setrole`.

Handy commands: `/security check` (explains exactly why a command is allowed or denied for anyone), `/security allow|deny|remove|rules`,
`/security blacklist`, `/security lockdown`, `/security audit`, `/security logchannel`, `/security antiraid`, `/security overview`.

Other protections: per-user throttle, auto-cooldown after repeated denied attempts, bot can never ping @everyone/roles,
moderation role-hierarchy checks, confirmations for bans/purges, audit log in the database and in your log channel.
The `/security` commands are exempt from custom rules for admins, so an admin can't lock themselves out.

## 5. Features

- **Moderation** `/mod`: kick, ban, tempban, unban, timeout, warn, warnings, history, case, purge, slowmode, lock/unlock, nick
- **Utility**: userinfo, serverinfo, avatar, roleinfo, calc (safe parser), timestamp, roll, choose, coinflip, 8ball, reminders, to-do list, tags, AFK, "Bookmark Message" right-click action
- **Community**: persistent-button polls (live results, timed), giveaways (role requirement, reroll), welcome messages, auto-role, tickets
- **Anti-raid**: join-flood detection with alert / timeout / kick, minimum account age
- **Owner**: global blacklist, sync, reload, list/leave servers

## 6. The web dashboard

Open your Railway domain and sign in with `DASH_PASSWORD`.

- **Server icon**: upload (PNG/JPEG/WebP/GIF, up to 8 MB) or remove.
- **Channel editor**: drag the ⋮⋮ handles (or use the arrows / "move to" menu), rename in place, add categories, text and voice channels, mark things for deletion.
- **Auto-organizer (Groq)**: pick what it may do (fix spelling, add emojis, reorder, create missing channels), optionally describe your server, press Generate.
  Groq's result lands in the editor as a *draft*. Nothing changes on Discord. Edit anything it did (names, order, new channels), or Undo.
- **Review**: *Preview & apply* shows the exact list of changes. Apply is refused if the server or the draft changed since you reviewed it. Deleting needs an explicit tick-box.

Dashboard security: fails closed without a password; constant-time password check; per-IP and global login lockouts; random server-side sessions
in HttpOnly + SameSite=Strict (+Secure on HTTPS) cookies with idle and absolute timeouts; CSRF token + same-origin check on every change;
strict Content-Security-Policy; all names rendered as plain text; every draft re-validated server-side; checkboxes enforced server-side even if the AI ignores them;
channels are never deleted unless you explicitly mark them.

## 7. Local development (optional)

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
cp .env.example .env      # fill it in
python main.py
python -m pytest -q
```
