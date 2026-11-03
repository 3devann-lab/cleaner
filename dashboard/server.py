"""The dashboard web server (aiohttp), running inside the bot process.

Security model
--------------
* Fails closed: it only starts when DASH_PASSWORD is set and long enough.
* Password compared in constant time; failed logins are rate-limited per IP AND globally.
* Sessions are random 256-bit tokens kept server-side, sent as HttpOnly + SameSite=Strict
  (+ Secure on HTTPS) cookies, with an absolute and an idle timeout.
* Every state-changing request needs a per-session CSRF token header AND a same-origin
  Origin header (when the browser sends one).
* Strict Content-Security-Policy (no inline script/style), no framing, no sniffing.
* The AI and the client are never trusted: every draft is re-validated server-side, and
  Apply recomputes the diff and refuses if it differs from what was reviewed.
* Channel deletion needs an explicit confirmation flag.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
import discord

import config
from dashboard import groq_ai, guild_ops, layout

log = logging.getLogger("dashboard")

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "dash_session"
IDLE_TIMEOUT = 3 * 3600
MAX_JSON_BYTES = 1_000_000
MAX_ICON_BYTES = 8 * 1024 * 1024
LOGIN_FAILS_PER_IP, LOGIN_WINDOW, LOGIN_LOCK = 5, 900, 900
LOGIN_FAILS_GLOBAL, GLOBAL_LOCK = 40, 300
AI_CALLS_PER_MINUTE = 6

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https://cdn.discordapp.com blob: data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


class State:
    def __init__(self):
        self.sessions = {}  # token -> {"csrf", "created", "last", "ai": deque}
        self.fails = defaultdict(deque)  # ip -> deque[timestamps]
        self.locked_until = {}  # ip -> ts
        self.global_fails = deque()
        self.global_lock = 0.0
        self.jobs = {}
        self.locks = defaultdict(asyncio.Lock)  # guild_id -> lock
        self.http = None  # shared aiohttp client session (Groq)


def client_ip(request: web.Request) -> str:
    # Behind Railway's proxy the right-most X-Forwarded-For entry is the one its edge appended.
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:64]
    return request.remote or "unknown"


def is_https(request: web.Request) -> bool:
    return request.headers.get("X-Forwarded-Proto", request.scheme) == "https"


def json_error(message: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _purge(state: State, now: float) -> None:
    for token in [t for t, s in state.sessions.items() if now - s["created"] > config.DASH_SESSION_HOURS * 3600 or now - s["last"] > IDLE_TIMEOUT]:
        state.sessions.pop(token, None)
    for job_id in [j for j, job in state.jobs.items() if now - job["created"] > 3600][:50]:
        state.jobs.pop(job_id, None)


# ------------------------------------------------------------ middlewares
@web.middleware
async def guard_middleware(request: web.Request, handler):
    state: State = request.app["state"]
    path = request.path
    try:
        # Same-origin check for anything that changes state
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                raise ApiError("Cross-origin request blocked.", 403)

        request["session"] = None
        needs_auth = path.startswith("/api/") or path == "/app" or path == "/static/app.js"
        if needs_auth:
            now = time.time()
            token = request.cookies.get(COOKIE)
            session = state.sessions.get(token) if token else None
            if session and (now - session["created"] > config.DASH_SESSION_HOURS * 3600 or now - session["last"] > IDLE_TIMEOUT):
                state.sessions.pop(token, None)
                session = None
            if session is None:
                if path.startswith("/api/"):
                    raise ApiError("Not signed in.", 401)
                raise web.HTTPFound("/login")
            session["last"] = now
            request["session"] = session
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                supplied = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(supplied.encode(), session["csrf"].encode()):
                    raise ApiError("Invalid CSRF token. Reload the page.", 403)
        return await handler(request)
    except ApiError as exc:
        return json_error(exc.message, exc.status, **exc.extra)


@web.middleware
async def headers_middleware(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _add_headers(exc, request)
        raise
    _add_headers(response, request)
    return response


def _add_headers(response, request) -> None:
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    # NOT "no-referrer": browsers then send "Origin: null" on POSTs, which our same-origin check rejects.
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    h["Cache-Control"] = "no-store"
    if is_https(request):
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


# ----------------------------------------------------------------- pages
def _read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


async def index(request: web.Request):
    token = request.cookies.get(COOKIE)
    if token and token in request.app["state"].sessions:
        raise web.HTTPFound("/app")
    raise web.HTTPFound("/login")


async def login_page(request: web.Request):
    messages = {"1": "Wrong password.", "locked": "Too many attempts. Try again later."}
    message = messages.get(request.query.get("e", ""), "")
    html = _read_static("login.html").replace("{{MESSAGE}}", message).replace("{{HIDDEN}}", "" if message else "hidden")
    return web.Response(text=html, content_type="text/html")


async def login_submit(request: web.Request):
    state: State = request.app["state"]
    ip, now = client_ip(request), time.time()
    _purge(state, now)

    if state.locked_until.get(ip, 0) > now or state.global_lock > now:
        raise web.HTTPFound("/login?e=locked")

    form = await request.post()
    supplied = form.get("password", "")
    supplied = supplied if isinstance(supplied, str) else ""
    # Compare fixed-length digests so neither content nor length leaks through timing
    ok = hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(config.DASH_PASSWORD.encode()).digest())

    if not ok:
        fails = state.fails[ip]
        fails.append(now)
        while fails and now - fails[0] > LOGIN_WINDOW:
            fails.popleft()
        if len(fails) >= LOGIN_FAILS_PER_IP:
            state.locked_until[ip] = now + LOGIN_LOCK
            fails.clear()
        state.global_fails.append(now)
        while state.global_fails and now - state.global_fails[0] > LOGIN_WINDOW:
            state.global_fails.popleft()
        if len(state.global_fails) >= LOGIN_FAILS_GLOBAL:
            state.global_lock = now + GLOBAL_LOCK
            state.global_fails.clear()
            log.warning("Dashboard login locked globally after repeated failures")
        log.warning("Failed dashboard login from %s", ip)
        await asyncio.sleep(1.0)  # slow down guessing
        raise web.HTTPFound("/login?e=1")

    state.fails.pop(ip, None)
    token = secrets.token_urlsafe(32)
    state.sessions[token] = {"csrf": secrets.token_urlsafe(24), "created": now, "last": now, "ai": deque()}
    response = web.HTTPFound("/app")
    response.set_cookie(
        COOKIE, token, httponly=True, secure=is_https(request), samesite="Strict",
        max_age=config.DASH_SESSION_HOURS * 3600, path="/",
    )
    log.info("Dashboard login from %s", ip)
    raise response


async def logout(request: web.Request):
    request.app["state"].sessions.pop(request.cookies.get(COOKIE), None)
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


async def app_page(request: web.Request):
    return web.Response(text=_read_static("app.html"), content_type="text/html")


async def static_file(request: web.Request):
    name = request.match_info["name"]
    if name not in {"app.js", "style.css"}:
        raise web.HTTPNotFound()
    return web.Response(text=_read_static(name), content_type="application/javascript" if name.endswith(".js") else "text/css")


async def healthz(request: web.Request):
    return web.Response(text="ok")


# ------------------------------------------------------------------- API
def _bot(request) -> "discord.Client":
    return request.app["bot"]


def _guild(request: web.Request) -> discord.Guild:
    gid = request.match_info["gid"]
    if not gid.isdigit():
        raise ApiError("Invalid server ID.", 400)
    if config.DASH_GUILD_IDS and int(gid) not in config.DASH_GUILD_IDS:
        raise ApiError("That server isn't enabled for the dashboard.", 403)
    guild = _bot(request).get_guild(int(gid))
    if guild is None:
        raise ApiError("The bot isn't in that server.", 404)
    return guild


async def _json(request: web.Request) -> dict:
    if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
        raise ApiError("Request too large.", 413)
    # read() may return a partial body, so loop until EOF while enforcing the cap
    chunks, size = [], 0
    async for chunk in request.content.iter_chunked(65536):
        size += len(chunk)
        if size > MAX_JSON_BYTES:
            raise ApiError("Request too large.", 413)
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise ApiError("Invalid JSON.", 400) from None
    if not isinstance(data, dict):
        raise ApiError("Invalid JSON.", 400)
    return data


def _need_perm(guild: discord.Guild, *names: str) -> None:
    perms = guild.me.guild_permissions
    if perms.administrator:
        return
    missing = [n.replace("_", " ") for n in names if not getattr(perms, n)]
    if missing:
        raise ApiError("The bot is missing permission: " + ", ".join(missing).title() + ".", 400)


async def api_session(request):
    return web.json_response({
        "csrf": request["session"]["csrf"],
        "groq": bool(config.GROQ_API_KEY),
        "model": config.GROQ_MODEL,
    })


async def api_guilds(request):
    guilds = [g for g in _bot(request).guilds if not config.DASH_GUILD_IDS or g.id in config.DASH_GUILD_IDS]
    guilds.sort(key=lambda g: g.name.lower())
    return web.json_response({"guilds": [guild_ops.guild_info(g) for g in guilds]})


async def api_layout(request):
    guild = _guild(request)
    snap = guild_ops.snapshot(guild)
    return web.json_response({"guild": guild_ops.guild_info(guild), "layout": snap, "draft": layout.snapshot_to_draft(snap)})


def _review(guild, draft) -> dict:
    snap = guild_ops.snapshot(guild)
    try:
        result = layout.validate_draft(draft, snap)
    except layout.LayoutError as exc:
        raise ApiError(str(exc)) from None
    ops = layout.compute_diff(snap, result["draft"]) if not result["errors"] else []
    return {"snap": snap, "result": result, "ops": ops, "fingerprint": layout.fingerprint(ops)}


async def api_preview(request):
    guild = _guild(request)
    data = await _json(request)
    review = _review(guild, data.get("draft"))
    return web.json_response({
        "draft": review["result"]["draft"], "errors": review["result"]["errors"],
        "warnings": review["result"]["warnings"], "ops": review["ops"], "fingerprint": review["fingerprint"],
    })


async def api_ai(request):
    state: State = request.app["state"]
    guild = _guild(request)
    if not config.GROQ_API_KEY:
        raise ApiError("GROQ_API_KEY isn't set on the server, so the AI organizer is off.", 400)
    now = time.time()
    calls = request["session"]["ai"]
    while calls and now - calls[0] > 60:
        calls.popleft()
    if len(calls) >= AI_CALLS_PER_MINUTE:
        raise ApiError("Slow down: too many AI requests this minute.", 429)
    calls.append(now)

    data = await _json(request)
    opts = groq_ai.parse_options(data.get("options"))
    snap = guild_ops.snapshot(guild)
    try:
        result = layout.validate_draft(data.get("draft"), snap)
    except layout.LayoutError as exc:
        raise ApiError(str(exc)) from None
    if result["errors"]:
        raise ApiError("Fix these first: " + " ".join(result["errors"]))
    base = result["draft"]
    ai_input, amap, count = groq_ai.build_ai_input(base)
    if count > groq_ai.MAX_AI_CHANNELS:
        raise ApiError(f"The AI organizer supports up to {groq_ai.MAX_AI_CHANNELS} channels (this server has {count}). Organise it manually.")
    if count == 0 and not opts["create_new"]:
        raise ApiError("There's nothing to organise.")

    if state.http is None:
        state.http = aiohttp.ClientSession()
    try:
        raw = await groq_ai.request_layout(state.http, config.GROQ_API_KEY, config.GROQ_MODEL, groq_ai.build_messages(ai_input, opts, guild.name))
        merged, notes, merge_warnings = groq_ai.merge_ai_output(base, raw, amap, opts)
    except groq_ai.GroqError as exc:
        raise ApiError(str(exc), 502) from None
    merged["deleted"] = [{"id": d["id"]} for d in base["deleted"]]
    final = layout.validate_draft(merged, snap)
    log.info("Groq suggestion for guild %s: %s item(s)", guild.id, count)
    return web.json_response({
        "draft": final["draft"], "notes": notes, "warnings": merge_warnings + final["warnings"], "errors": final["errors"],
    })


async def api_apply(request):
    state: State = request.app["state"]
    guild = _guild(request)
    data = await _json(request)
    _need_perm(guild, "manage_channels")
    review = _review(guild, data.get("draft"))
    if review["result"]["errors"]:
        raise ApiError("Fix these first: " + " ".join(review["result"]["errors"]))
    if not review["ops"]:
        raise ApiError("There's nothing to apply.")
    if data.get("fingerprint") != review["fingerprint"]:
        raise ApiError("The server changed (or the draft did) since you reviewed it. Review the changes again.", 409)
    if any(op["kind"] == "delete" for op in review["ops"]) and data.get("confirm_deletes") is not True:
        raise ApiError("Deleting channels needs explicit confirmation.", 400)
    lock = state.locks[guild.id]
    if lock.locked():
        raise ApiError("Another change is already running for this server.", 409)
    await lock.acquire()  # no await between the check and this, so two clicks can't both get in

    bot = _bot(request)
    job_id = secrets.token_urlsafe(12)
    job = {"guild_id": guild.id, "status": "running", "results": [], "total": len(review["ops"]), "created": time.time(), "summary": ""}
    state.jobs[job_id] = job
    sync = data.get("sync_permissions") is True

    async def run():
        try:
            results = await guild_ops.apply_layout(
                bot, guild, review["snap"], review["result"]["draft"],
                sync_permissions=sync, progress=lambda entry: job["results"].append(entry),
            )
            failed = sum(1 for r in results if not r["ok"])
            job["status"] = "done"
            job["summary"] = f"{len(results) - failed} step(s) succeeded, {failed} failed."
            await bot.security.audit(
                guild.id, None, "dashboard:organize", None,
                f"{len(review['ops'])} change(s) applied from the web dashboard. {job['summary']}",
            )
        except Exception:
            log.exception("Dashboard apply failed")
            job["status"] = "error"
            job["summary"] = "Something went wrong while applying. Check the Railway logs."
        finally:
            lock.release()

    request.app["tasks"].add(task := asyncio.create_task(run()))
    task.add_done_callback(request.app["tasks"].discard)
    return web.json_response({"job": job_id})


async def api_job(request):
    job = request.app["state"].jobs.get(request.match_info["job"])
    if job is None:
        raise ApiError("Unknown job.", 404)
    return web.json_response({k: job[k] for k in ("status", "results", "total", "summary")})


def _image_type(raw: bytes):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw[:4] == b"GIF8":
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


async def api_icon(request):
    guild = _guild(request)
    _need_perm(guild, "manage_guild")
    form = await request.post()
    if form.get("remove") == "1":
        data = None
    else:
        upload = form.get("icon")
        if not isinstance(upload, web.FileField):
            raise ApiError("Choose an image first.")
        raw = upload.file.read(MAX_ICON_BYTES + 1)
        if len(raw) > MAX_ICON_BYTES:
            raise ApiError("That image is too large (max 8 MB).")
        kind = _image_type(raw)
        if kind is None:
            raise ApiError("Use a PNG, JPEG, GIF or WebP image.")
        data = raw
    try:
        await guild_ops.set_icon(guild, data)
    except discord.HTTPException as exc:
        raise ApiError("Discord refused the icon: " + ((getattr(exc, "text", "") or str(exc))[:150]), 400) from None
    await _bot(request).security.audit(guild.id, None, "dashboard:icon", None, "Server icon removed" if data is None else "Server icon replaced")
    return web.json_response({"guild": guild_ops.guild_info(guild)})


# ---------------------------------------------------------------- startup
async def start_dashboard(bot) -> web.AppRunner:
    app = web.Application(middlewares=[headers_middleware, guard_middleware], client_max_size=MAX_ICON_BYTES + 1024 * 1024)
    app["bot"] = bot
    app["state"] = State()
    app["tasks"] = set()  # strong references so background apply jobs are never garbage-collected
    app.add_routes([
        web.get("/", index),
        web.get("/healthz", healthz),
        web.get("/login", login_page),
        web.post("/login", login_submit),
        web.post("/api/logout", logout),
        web.get("/app", app_page),
        web.get("/static/{name}", static_file),
        web.get("/api/session", api_session),
        web.get("/api/guilds", api_guilds),
        web.get("/api/guilds/{gid}/layout", api_layout),
        web.post("/api/guilds/{gid}/preview", api_preview),
        web.post("/api/guilds/{gid}/ai", api_ai),
        web.post("/api/guilds/{gid}/apply", api_apply),
        web.post("/api/guilds/{gid}/icon", api_icon),
        web.get("/api/jobs/{job}", api_job),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("Dashboard listening on port %s", config.PORT)
    return runner


async def stop_dashboard(runner: web.AppRunner) -> None:
    state = runner.app["state"]
    if state.http is not None:
        await state.http.close()
    await runner.cleanup()
