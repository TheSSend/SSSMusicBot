import os
import json
import logging
from pathlib import Path

import discord
from aiohttp import web

from json_store import JsonStore
from runtime_paths import data_path


logger = logging.getLogger(__name__)

def _csv_ints(value: str) -> list[int]:
    parts = [p.strip() for p in (value or "").split(",")]
    return [int(p) for p in parts if p.isdigit()]


def _int_or_zero(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


def _require_token(request: web.Request) -> None:
    expected = os.getenv("WEB_ADMIN_TOKEN", "")
    if not expected:
        raise web.HTTPUnauthorized(text="WEB_ADMIN_TOKEN is not set")

    provided = request.query.get("token")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    if provided != expected:
        raise web.HTTPUnauthorized(text="Invalid token")


def _log_file_path() -> Path:
    log_dir = Path(os.getenv("MUSICBOT_LOG_DIR", "logs"))
    return log_dir / "bot.log"


async def _index(request: web.Request) -> web.Response:
    _require_token(request)
    bot = request.app["bot"]
    token = request.query.get("token", "")

    guild_count = len(getattr(bot, "guilds", []) or [])
    users = sum(getattr(guild, "member_count", 0) or 0 for guild in getattr(bot, "guilds", []) or [])
    ext_names = sorted(getattr(bot, "extensions", {}).keys())

    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Musicbot Admin</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 20px; }}
    code, pre {{ background: #f4f4f4; padding: 2px 4px; border-radius: 4px; }}
    pre {{ padding: 12px; overflow: auto; }}
    .row {{ margin: 8px 0; }}
    input {{ padding: 6px; }}
    button {{ padding: 6px 10px; }}
  </style>
</head>
<body>
  <h1>Musicbot Admin</h1>
  <div class="row">Guilds: <b>{guild_count}</b> | Users: <b>{users}</b></div>

  <h2>Extensions</h2>
  <pre>{json.dumps(ext_names, ensure_ascii=False, indent=2)}</pre>

  <h3>Reload extension</h3>
  <form method="post" action="/reload?token={token}">
    <input name="extension" placeholder="giveaway / signups / ..." />
    <button type="submit">Reload</button>
  </form>

  <h2>Settings</h2>
  <div class="row"><a href="/settings?token={token}">Module settings (web_config.json)</a></div>

  <h2>Sync</h2>
  <form method="post" action="/sync?token={token}">
    <button type="submit">Sync commands</button>
  </form>

  <h2>Logs</h2>
  <div class="row"><a href="/logs?n=200&token={token}">Last 200 lines</a></div>

  <h2>Config</h2>
  <div class="row"><a href="/config?token={token}">View config (JSON)</a></div>
</body>
</html>
"""
    return web.Response(text=body, content_type="text/html")


async def _logs(request: web.Request) -> web.Response:
    _require_token(request)
    n = int(request.query.get("n", "200"))
    n = max(10, min(n, 5000))

    path = _log_file_path()
    if not path.exists():
        return web.Response(text=f"Log file not found: {path}\n", content_type="text/plain")

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return web.Response(text=f"Failed to read log: {exc}\n", content_type="text/plain", status=500)

    tail = "\n".join(lines[-n:]) + "\n"
    return web.Response(text=tail, content_type="text/plain")


async def _config_get(request: web.Request) -> web.Response:
    _require_token(request)
    store: JsonStore = request.app["config_store"]
    web_config = store.load()

    safe_env = {
        "GUILD_ID": os.getenv("GUILD_ID") or None,
        "LAVALINK_HOST": os.getenv("LAVALINK_HOST") or None,
        "LAVALINK_PORT": os.getenv("LAVALINK_PORT") or None,
        "MUSICBOT_DATA_DIR": os.getenv("MUSICBOT_DATA_DIR") or None,
        "MUSICBOT_LOG_DIR": os.getenv("MUSICBOT_LOG_DIR") or None,
        "PLAYER_STATE_DUMP_INTERVAL": os.getenv("PLAYER_STATE_DUMP_INTERVAL") or None,
        "WEB_ADMIN_HOST": os.getenv("WEB_ADMIN_HOST") or None,
        "WEB_ADMIN_PORT": os.getenv("WEB_ADMIN_PORT") or None,
        "WEB_ADMIN_ENABLED": os.getenv("WEB_ADMIN_ENABLED") or None,
    }

    result = {
        "env": safe_env,
        "web_config": web_config,
        "log_file": str(_log_file_path()),
    }
    return web.json_response(result, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, indent=2))


async def _config_post(request: web.Request) -> web.Response:
    _require_token(request)
    store: JsonStore = request.app["config_store"]
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Expected JSON body")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="JSON body must be an object")
    store.save(payload)
    return web.json_response({"ok": True})

async def _settings_get(request: web.Request) -> web.Response:
    _require_token(request)
    token = request.query.get("token", "")

    store: JsonStore = request.app["config_store"]
    cfg = store.load()
    if not isinstance(cfg, dict):
        cfg = {}

    giveaway = cfg.get("giveaway", {}) if isinstance(cfg.get("giveaway"), dict) else {}
    gsay = cfg.get("gsay", {}) if isinstance(cfg.get("gsay"), dict) else {}
    joinfamily = cfg.get("joinfamily", {}) if isinstance(cfg.get("joinfamily"), dict) else {}
    signups = cfg.get("signups", {}) if isinstance(cfg.get("signups"), dict) else {}

    gsay_allowed_roles_value = (
        ",".join(str(x) for x in (gsay.get("allowed_roles") or []))
        if isinstance(gsay.get("allowed_roles"), list)
        else (gsay.get("allowed_roles") or "")
    )

    joinfamily_hr_access_value = (
        ",".join(str(x) for x in (joinfamily.get("hr_access") or []))
        if isinstance(joinfamily.get("hr_access"), list)
        else (joinfamily.get("hr_access") or "")
    )

    signups_managers_value = (
        ",".join(str(x) for x in (signups.get("managers") or []))
        if isinstance(signups.get("managers"), list)
        else (signups.get("managers") or "")
    )

    signups_admins_value = (
        ",".join(str(x) for x in (signups.get("admins") or []))
        if isinstance(signups.get("admins"), list)
        else (signups.get("admins") or "")
    )

    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Musicbot Settings</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 20px; max-width: 960px; }}
    input {{ width: 420px; padding: 6px; }}
    textarea {{ width: 100%; height: 220px; }}
    .row {{ margin: 10px 0; }}
    .hint {{ color: #666; font-size: 12px; }}
    .box {{ background: #f4f4f4; padding: 12px; border-radius: 8px; }}
  </style>
</head>
<body>
  <h1>Module settings (web_config.json)</h1>
  <div class="row"><a href="/?token={token}">Back</a></div>

  <div class="box">
    <div class="row hint">Формат: ID и списки через запятую. После сохранения нажмите “Reload” нужного модуля (или перезапустите бота), чтобы настройки применились.</div>
  </div>

  <form method="post" action="/settings/save?token={token}">
    <h2>Giveaway</h2>
    <div class="row">
      <label>admin_role_id</label><br />
      <input name="giveaway_admin_role_id" value="{giveaway.get("admin_role_id","")}" placeholder="1234567890" />
    </div>

    <h2>GSay</h2>
    <div class="row">
      <label>allowed_roles</label><br />
      <input name="gsay_allowed_roles" value="{gsay_allowed_roles_value}" placeholder="1,2,3" />
      <div class="hint">Роли, которые могут использовать /gsay (OWNER всегда может).</div>
    </div>

    <h2>JoinFamily</h2>
    <div class="row">
      <label>hr_access</label><br />
      <input name="joinfamily_hr_access" value="{joinfamily_hr_access_value}" placeholder="1,2,3" />
    </div>
    <div class="row">
      <label>log_channel_id</label><br />
      <input name="joinfamily_log_channel_id" value="{joinfamily.get('log_channel_id','')}" placeholder="1234567890" />
    </div>
    <div class="row">
      <label>remove_role_id</label><br />
      <input name="joinfamily_remove_role_id" value="{joinfamily.get('remove_role_id','')}" placeholder="1234567890" />
    </div>
    <div class="row">
      <label>add_role_1_id</label><br />
      <input name="joinfamily_add_role_1_id" value="{joinfamily.get('add_role_1_id','')}" placeholder="1234567890" />
    </div>
    <div class="row">
      <label>add_role_2_id</label><br />
      <input name="joinfamily_add_role_2_id" value="{joinfamily.get('add_role_2_id','')}" placeholder="1234567890" />
    </div>

    <h2>Signups</h2>
    <div class="row">
      <label>managers</label><br />
      <input name="signups_managers" value="{signups_managers_value}" placeholder="1,2,3" />
    </div>
    <div class="row">
      <label>admins</label><br />
      <input name="signups_admins" value="{signups_admins_value}" placeholder="1,2,3" />
    </div>

    <div class="row">
      <button type="submit">Save</button>
    </div>
  </form>

  <h2>Raw web_config.json</h2>
  <textarea readonly>{json.dumps(cfg, ensure_ascii=False, indent=2)}</textarea>
</body>
</html>
"""
    return web.Response(text=body, content_type="text/html")


async def _settings_save(request: web.Request) -> web.Response:
    _require_token(request)
    token = request.query.get("token", "")

    store: JsonStore = request.app["config_store"]
    cfg = store.load()
    if not isinstance(cfg, dict):
        cfg = {}

    data = await request.post()

    cfg.setdefault("giveaway", {})
    if not isinstance(cfg["giveaway"], dict):
        cfg["giveaway"] = {}
    cfg["giveaway"]["admin_role_id"] = _int_or_zero(str(data.get("giveaway_admin_role_id", ""))) or None

    cfg.setdefault("gsay", {})
    if not isinstance(cfg["gsay"], dict):
        cfg["gsay"] = {}
    cfg["gsay"]["allowed_roles"] = _csv_ints(str(data.get("gsay_allowed_roles", "")))

    cfg.setdefault("joinfamily", {})
    if not isinstance(cfg["joinfamily"], dict):
        cfg["joinfamily"] = {}
    cfg["joinfamily"]["hr_access"] = _csv_ints(str(data.get("joinfamily_hr_access", "")))
    cfg["joinfamily"]["log_channel_id"] = _int_or_zero(str(data.get("joinfamily_log_channel_id", ""))) or None
    cfg["joinfamily"]["remove_role_id"] = _int_or_zero(str(data.get("joinfamily_remove_role_id", ""))) or None
    cfg["joinfamily"]["add_role_1_id"] = _int_or_zero(str(data.get("joinfamily_add_role_1_id", ""))) or None
    cfg["joinfamily"]["add_role_2_id"] = _int_or_zero(str(data.get("joinfamily_add_role_2_id", ""))) or None

    cfg.setdefault("signups", {})
    if not isinstance(cfg["signups"], dict):
        cfg["signups"] = {}
    cfg["signups"]["managers"] = _csv_ints(str(data.get("signups_managers", "")))
    cfg["signups"]["admins"] = _csv_ints(str(data.get("signups_admins", "")))

    store.save(cfg)
    raise web.HTTPFound(location=f"/settings?token={token}")


async def _reload(request: web.Request) -> web.Response:
    _require_token(request)
    bot: discord.Client = request.app["bot"]
    data = await request.post()
    extension = (data.get("extension") or "").strip()
    if not extension:
        raise web.HTTPBadRequest(text="Missing 'extension' form field")

    try:
        await bot.reload_extension(extension)
    except Exception as exc:
        logger.exception("Web reload failed for %s", extension)
        return web.Response(text=f"Reload failed: {exc}\n", content_type="text/plain", status=500)
    return web.Response(text=f"Reloaded: {extension}\n", content_type="text/plain")

async def _sync(request: web.Request) -> web.Response:
    _require_token(request)
    bot = request.app["bot"]

    guild_id_str = os.getenv("GUILD_ID")
    try:
        if guild_id_str and guild_id_str.isdigit():
            guild = discord.Object(id=int(guild_id_str))
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
    except Exception as exc:
        logger.exception("Web sync failed")
        return web.Response(text=f"Sync failed: {exc}\n", content_type="text/plain", status=500)

    names = [getattr(cmd, "name", "?") for cmd in (synced or [])]
    return web.Response(text="Synced:\n" + "\n".join(names) + "\n", content_type="text/plain")


async def maybe_start_web_admin(bot: discord.Client) -> web.AppRunner | None:
    enabled = os.getenv("WEB_ADMIN_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None

    host = os.getenv("WEB_ADMIN_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_ADMIN_PORT", "8080"))

    store = JsonStore(data_path("web_config.json"))

    app = web.Application()
    app["bot"] = bot
    app["config_store"] = store

    app.add_routes(
        [
            web.get("/", _index),
            web.get("/logs", _logs),
            web.get("/config", _config_get),
            web.post("/config", _config_post),
            web.get("/settings", _settings_get),
            web.post("/settings/save", _settings_save),
            web.post("/reload", _reload),
            web.post("/sync", _sync),
        ]
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info("Web admin started on http://%s:%s (protected)", host, port)
    return runner
