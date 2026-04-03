import os
import json
import time
import html as html_lib
import logging
import base64
import binascii
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

import discord
import wavelink
from aiohttp import web

from json_store import JsonStore
from runtime_paths import data_path
from music_core import display_author


logger = logging.getLogger(__name__)
_BOOT_TS = time.time()

ENV_EDITABLE_FIELDS: list[tuple[str, str, str, bool, str]] = [
    ("DISCORD_TOKEN", "Discord token", "password", True, "Required. Restart required after change."),
    ("OWNER_ID", "Owner ID", "text", False, "Discord user ID."),
    ("GUILD_ID", "Guild ID", "text", False, "Optional guild-scoped command sync."),
    ("LAVALINK_HOST", "Lavalink host", "text", False, "Host or IP of Lavalink."),
    ("LAVALINK_PORT", "Lavalink port", "text", False, "Usually 2333."),
    ("LAVALINK_PASSWORD", "Lavalink password", "password", True, "Keep private."),
    ("MUSICBOT_DATA_DIR", "Data dir", "text", False, "Folder for JSON state files."),
    ("MUSICBOT_LOG_DIR", "Log dir", "text", False, "Folder for log files."),
    ("PLAYER_STATE_DUMP_INTERVAL", "Player state dump interval", "text", False, "Seconds."),
    ("LYRICS_CACHE_TTL_SECONDS", "Lyrics cache TTL", "text", False, "Seconds."),
    ("GENIUS_ACCESS_TOKEN", "Genius token", "password", True, "Enables Genius lyrics fallback."),
    ("WEB_ADMIN_ENABLED", "Web admin enabled", "text", False, "1/0, true/false."),
    ("WEB_ADMIN_HOST", "Web admin host", "text", False, "Use 0.0.0.0 for external access."),
    ("WEB_ADMIN_PORT", "Web admin port", "text", False, "Listening port."),
    ("WEB_ADMIN_TOKEN", "Web admin token", "password", True, "Bearer/token auth fallback."),
    ("WEB_ADMIN_BASIC_USER", "Web admin user", "text", False, "Browser login username."),
    ("WEB_ADMIN_BASIC_PASSWORD", "Web admin password", "password", True, "Browser login password."),
    ("GIVEAWAY_ADMIN_ROLE_ID", "Giveaway admin role", "text", False, "Legacy env fallback."),
    ("GSAY_ALLOWED_ROLES", "GSay allowed roles", "text", False, "Comma-separated IDs."),
    ("HR_ACCESS", "JoinFamily HR access", "text", False, "Comma-separated IDs."),
    ("FAMILY_CALL_CHANNELS", "Family call channels", "text", False, "Comma-separated IDs."),
    ("FAMILY_LOG_CHANNEL", "Family log channel", "text", False, "Legacy env fallback."),
    ("FAMILY_REMOVE_ROLE_ID", "Family remove role", "text", False, "Legacy env fallback."),
    ("FAMILY_ADD_ROLE_1_ID", "Family add role 1", "text", False, "Legacy env fallback."),
    ("FAMILY_ADD_ROLE_2_ID", "Family add role 2", "text", False, "Legacy env fallback."),
    ("SIGNUP_MANAGERS", "Signups managers", "text", False, "Comma-separated IDs."),
    ("SIGNUP_ADMINS", "Signups admins", "text", False, "Comma-separated IDs."),
]

ENV_FIELD_MAP = {name: (label, input_type, secret, hint) for name, label, input_type, secret, hint in ENV_EDITABLE_FIELDS}

def _csv_ints(value: str) -> list[int]:
    parts = [p.strip() for p in (value or "").split(",")]
    return [int(p) for p in parts if p.isdigit()]


def _int_or_zero(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


def _esc(value: object) -> str:
    return html_lib.escape("" if value is None else str(value), quote=True)


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"{value[:2]}••••{value[-2:]}"


def _format_duration_ms(value: int | float | None) -> str:
    if value is None:
        return "—"
    total_seconds = max(0, int(value // 1000 if value > 1000 else value))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _field_input(name: str, value: str | None, input_type: str = "text", placeholder: str = "", secret: bool = False) -> str:
    rendered_value = "" if secret else (value or "")
    safe_placeholder = _esc(placeholder)
    safe_value = _esc(rendered_value)
    return f'<input name="{_esc(name)}" type="{input_type}" value="{safe_value}" placeholder="{safe_placeholder}" />'


def _page(title: str, token: str, body: str, extra_head: str = "") -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --bg: #0b1220;
      --panel: #101a2f;
      --panel-2: #14203a;
      --border: rgba(148, 163, 184, 0.18);
      --text: #e5eefc;
      --muted: #9fb0ca;
      --accent: #38bdf8;
      --accent-2: #22c55e;
      --danger: #ef4444;
      --warning: #f59e0b;
      --shadow: 0 12px 30px rgba(2, 6, 23, 0.35);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: radial-gradient(circle at top left, #12203c, var(--bg) 38%);
      color: var(--text);
    }}
    a {{ color: #7dd3fc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .wrap {{ max-width: 1300px; margin: 0 auto; padding: 24px; }}
    .topbar {{
      display: flex; justify-content: space-between; gap: 16px; align-items: center;
      margin-bottom: 20px; padding: 18px 20px;
      background: rgba(16, 26, 47, 0.8); border: 1px solid var(--border);
      border-radius: 18px; box-shadow: var(--shadow); backdrop-filter: blur(10px);
    }}
    .brand h1 {{ margin: 0; font-size: 28px; }}
    .brand p {{ margin: 6px 0 0; color: var(--muted); }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
    .chip {{
      display: inline-flex; align-items: center; gap: 6px; padding: 8px 12px;
      border-radius: 999px; background: rgba(59, 130, 246, 0.14);
      border: 1px solid rgba(59, 130, 246, 0.25); color: var(--text); font-size: 13px;
    }}
    .grid {{
      display: grid; gap: 18px;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    }}
    .card {{
      background: rgba(16, 26, 47, 0.92); border: 1px solid var(--border);
      border-radius: 18px; padding: 18px; box-shadow: var(--shadow);
    }}
    .card h2, .card h3 {{ margin: 0 0 12px; }}
    .subtle {{ color: var(--muted); font-size: 14px; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; }}
    .stat {{
      padding: 14px; border-radius: 14px; background: rgba(20, 32, 58, 0.92);
      border: 1px solid var(--border);
    }}
    .stat .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .stat .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .stat .hint {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .section-title {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 12px; }}
    .btn, button {{
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 10px 14px; border-radius: 12px; border: 1px solid transparent;
      background: linear-gradient(180deg, rgba(56, 189, 248, 0.95), rgba(37, 99, 235, 0.95));
      color: white; font-weight: 600; cursor: pointer; text-decoration: none;
    }}
    .btn.secondary {{
      background: rgba(20, 32, 58, 0.95); border-color: var(--border); color: var(--text);
    }}
    .btn.success {{ background: linear-gradient(180deg, rgba(34, 197, 94, 0.95), rgba(21, 128, 61, 0.95)); }}
    .btn.danger {{ background: linear-gradient(180deg, rgba(248, 113, 113, 0.95), rgba(220, 38, 38, 0.95)); }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .field-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }}
    .field {{
      display: flex; flex-direction: column; gap: 6px; padding: 12px;
      background: rgba(20, 32, 58, 0.9); border-radius: 14px; border: 1px solid var(--border);
    }}
    .field label {{ font-size: 13px; color: var(--muted); }}
    .field input, .field textarea, .field select {{
      width: 100%; padding: 11px 12px; border-radius: 12px;
      background: rgba(8, 15, 28, 0.95); color: var(--text);
      border: 1px solid rgba(148, 163, 184, 0.22);
    }}
    .field textarea {{ min-height: 160px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .hint {{ color: var(--muted); font-size: 12px; }}
    table {{ width: 100%; border-collapse: collapse; overflow: hidden; }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(148, 163, 184, 0.12); vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    pre {{
      margin: 0; padding: 14px; border-radius: 14px; overflow: auto;
      background: rgba(8, 15, 28, 0.95); border: 1px solid var(--border);
      max-height: 420px;
    }}
    details summary {{ cursor: pointer; color: #7dd3fc; }}
    .badge {{
      display: inline-flex; align-items: center; padding: 4px 8px; border-radius: 999px;
      font-size: 12px; font-weight: 700; background: rgba(148, 163, 184, 0.16);
      border: 1px solid rgba(148, 163, 184, 0.22);
    }}
    .badge.green {{ background: rgba(34, 197, 94, 0.16); border-color: rgba(34, 197, 94, 0.26); }}
    .badge.red {{ background: rgba(239, 68, 68, 0.16); border-color: rgba(239, 68, 68, 0.26); }}
    .badge.yellow {{ background: rgba(245, 158, 11, 0.16); border-color: rgba(245, 158, 11, 0.26); }}
    .muted {{ color: var(--muted); }}
    .footer {{ margin-top: 20px; color: var(--muted); font-size: 12px; }}
    {extra_head}
  </style>
</head>
<body>
  <div class="wrap">
    {body}
  </div>
</body>
</html>
"""


def _env_file_path() -> Path:
    return Path(os.getenv("MUSICBOT_ENV_FILE", ".env"))


def _read_env_file() -> tuple[list[str], dict[str, str], set[str]]:
    path = _env_file_path()
    if not path.exists():
        return [], {}, set()

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    values: dict[str, str] = {}
    keys_in_file: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        if not line or stripped.startswith("#") or "=" not in line or stripped.startswith("export "):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        values[key] = value
        keys_in_file.add(key)
    return lines, values, keys_in_file


def _write_env_file(updates: dict[str, str]) -> None:
    path = _env_file_path()
    lines, current, keys_in_file = _read_env_file()
    current.update(updates)

    if not lines and not path.exists():
        lines = []

    rewritten: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        if line and not stripped.startswith("#") and "=" in line and not stripped.startswith("export "):
            key, _ = line.split("=", 1)
            key = key.strip()
            if key in updates:
                rewritten.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                rewritten.append(line)
        else:
            rewritten.append(line)

    for key, value in updates.items():
        if key not in keys_in_file and key not in seen:
            rewritten.append(f"{key}={value}")

    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    os.environ.update(updates)


def _current_env_snapshot(mask_secrets: bool = True) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for name, _, _, secret, _ in ENV_EDITABLE_FIELDS:
        value = os.getenv(name) or None
        if mask_secrets and secret and value:
            snapshot[name] = _mask_secret(value)
        else:
            snapshot[name] = value
    return snapshot


def _collect_runtime_status(bot: discord.Client) -> dict[str, object]:
    node = None
    try:
        node = wavelink.Pool.get_node()
    except Exception:
        node = None

    players = []
    if node is not None:
        for player in getattr(node, "players", {}).values():
            track = getattr(player, "current_track", None)
            requester = getattr(track, "requester", None) if track else None
            queue_items = []
            try:
                queue_items = list(player.queue)[:10]
            except Exception:
                queue_items = []
            try:
                queue_size = len(player.queue)
            except Exception:
                queue_size = len(queue_items)
            players.append(
                {
                    "guild_id": getattr(getattr(player, "guild", None), "id", None),
                    "guild_name": getattr(getattr(player, "guild", None), "name", None),
                    "channel_name": getattr(getattr(player, "channel", None), "name", None),
                    "control_message_id": getattr(getattr(player, "control_message", None), "id", None),
                    "text_channel_id": getattr(getattr(getattr(player, "control_message", None), "channel", None), "id", None),
                    "playing": bool(getattr(player, "playing", False)),
                    "paused": bool(getattr(player, "paused", False)),
                    "volume": getattr(player, "volume", None),
                    "queue_size": queue_size,
                    "track": {
                        "title": getattr(track, "title", None),
                        "author": display_author(getattr(track, "author", None)) if track else None,
                        "duration": _format_duration_ms(getattr(track, "length", None)) if track else None,
                        "position": _format_duration_ms(getattr(player, "position", None)),
                    } if track else None,
                    "current_track": {
                        "title": getattr(track, "title", None),
                        "author": display_author(getattr(track, "author", None)) if track else None,
                        "duration": _format_duration_ms(getattr(track, "length", None)) if track else None,
                        "position": _format_duration_ms(getattr(player, "position", None)),
                        "requester": getattr(requester, "mention", None)
                        or getattr(requester, "display_name", None)
                        or getattr(requester, "name", None)
                        or None,
                        "source": getattr(track, "uri", None),
                    } if track else None,
                    "queue_preview": [getattr(item, "title", str(item)) for item in queue_items],
                }
            )

    return {
        "uptime": _format_uptime(time.time() - _BOOT_TS),
        "latency_ms": round((bot.latency or 0) * 1000, 1) if getattr(bot, "latency", None) is not None else None,
        "guild_count": len(getattr(bot, "guilds", []) or []),
        "user_count": sum(getattr(guild, "member_count", 0) or 0 for guild in getattr(bot, "guilds", []) or []),
        "extensions": sorted(getattr(bot, "extensions", {}).keys()),
        "voice_clients": len(getattr(bot, "voice_clients", []) or []),
        "is_closed": bool(getattr(bot, "is_closed", lambda: False)()),
        "ws_connected": getattr(bot, "ws", None) is not None,
        "node_identifier": getattr(node, "identifier", None),
        "node_players": len(getattr(node, "players", {}) or {}) if node is not None else 0,
        "players": players,
    }


def _render_status_cards(status: dict[str, object]) -> str:
    cards = [
        ("Uptime", status.get("uptime") or "—", "Process age"),
        ("Latency", f"{status.get('latency_ms') or '—'} ms", "Discord gateway"),
        ("Guilds", str(status.get("guild_count") or 0), "Connected servers"),
        ("Users", str(status.get("user_count") or 0), "Cached member count"),
        ("Players", str(status.get("node_players") or 0), "Active music sessions"),
        ("Voice", str(status.get("voice_clients") or 0), "Discord voice clients"),
    ]
    rendered = []
    for label, value, hint in cards:
        rendered.append(
            f'<div class="stat"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div><div class="hint">{_esc(hint)}</div></div>'
        )
    return "".join(rendered)


def _render_player_rows(players: list[dict[str, object]]) -> str:
    if not players:
        return '<div class="subtle">No active music players.</div>'
    rows = []
    for player in players:
        track = player.get("track") or {}
        current_track = player.get("current_track") or {}
        queue_preview = player.get("queue_preview") or []
        queue_preview_text = "\n".join(queue_preview) or "Queue empty."
        requester_value = current_track.get("requester") or "—"
        control_value = f"#{player.get('text_channel_id') or '—'} / msg {player.get('control_message_id') or '—'}"
        rows.append(
            "<tr>"
            f"<td>{_esc(player.get('guild_name') or player.get('guild_id') or '—')}</td>"
            f"<td>{_esc(player.get('channel_name') or '—')}</td>"
            f"<td>{_esc(current_track.get('title') or track.get('title') or '—')}<div class='muted'>{_esc(current_track.get('author') or track.get('author') or '')}</div>"
            f"<div class='muted'>Requester: {_esc(requester_value)}</div></td>"
            f"<td>{_esc(current_track.get('position') or track.get('position') or '—')} / {_esc(current_track.get('duration') or track.get('duration') or '—')}</td>"
            f"<td>{_esc(player.get('queue_size') or 0)}</td>"
            f"<td>{_esc('playing' if player.get('playing') else 'paused' if player.get('paused') else 'idle')}<div class='muted'>{_esc('vol ' + str(player.get('volume')) if player.get('volume') is not None else 'vol —')}</div></td>"
            f"<td><div class='muted'>{_esc(control_value)}</div><details><summary>Preview</summary><pre>{_esc(queue_preview_text)}</pre></details></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Guild</th><th>Voice channel</th><th>Current track</th><th>Progress</th>"
        "<th>Queue</th><th>State</th><th>Controls / Queue preview</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_env_sections() -> str:
    current = _current_env_snapshot()
    sections = {
        "Core": ["DISCORD_TOKEN", "OWNER_ID", "GUILD_ID", "MUSICBOT_DATA_DIR", "MUSICBOT_LOG_DIR"],
        "Music": ["LAVALINK_HOST", "LAVALINK_PORT", "LAVALINK_PASSWORD", "PLAYER_STATE_DUMP_INTERVAL", "LYRICS_CACHE_TTL_SECONDS", "GENIUS_ACCESS_TOKEN"],
        "Web admin": ["WEB_ADMIN_ENABLED", "WEB_ADMIN_HOST", "WEB_ADMIN_PORT", "WEB_ADMIN_TOKEN", "WEB_ADMIN_BASIC_USER", "WEB_ADMIN_BASIC_PASSWORD"],
        "Legacy module env": ["GIVEAWAY_ADMIN_ROLE_ID", "GSAY_ALLOWED_ROLES", "HR_ACCESS", "FAMILY_CALL_CHANNELS", "FAMILY_LOG_CHANNEL", "FAMILY_REMOVE_ROLE_ID", "FAMILY_ADD_ROLE_1_ID", "FAMILY_ADD_ROLE_2_ID", "SIGNUP_MANAGERS", "SIGNUP_ADMINS"],
    }

    parts: list[str] = []
    for title, keys in sections.items():
        fields = []
        for key in keys:
            label, input_type, secret, hint = ENV_FIELD_MAP[key]
            value = current.get(key) or ""
            display_value = "" if secret else value
            badge = "configured" if value else "empty"
            fields.append(
                "<div class='field'>"
                f"<label>{_esc(label)} <span class='badge'>{_esc(badge)}</span></label>"
                f"{_field_input(key, display_value, input_type=input_type, placeholder='leave blank to keep current' if secret else '', secret=secret)}"
                f"<div class='hint'>{_esc(hint)}</div>"
                "</div>"
            )
        parts.append(f"<div class='card'><div class='section-title'><h3>{_esc(title)}</h3></div><div class='field-grid'>{''.join(fields)}</div></div>")
    return "".join(parts)


def _render_dashboard(bot: discord.Client, token: str, store: JsonStore) -> str:
    status = _collect_runtime_status(bot)
    current_env = _current_env_snapshot()
    web_config = store.load()
    if not isinstance(web_config, dict):
        web_config = {}

    players_html = _render_player_rows(status["players"] if isinstance(status.get("players"), list) else [])
    env_status = [
        f"<span class='badge green'>Auth: Basic/token</span>",
        f"<span class='badge'>{_esc(os.getenv('WEB_ADMIN_HOST', '0.0.0.0'))}:{_esc(os.getenv('WEB_ADMIN_PORT', '8080'))}</span>",
        f"<span class='badge'>{_esc('restart required for .env changes')}</span>",
    ]

    body = f"""
    <div class="topbar">
      <div class="brand">
        <h1>Musicbot Admin</h1>
        <p>Dashboard, settings, live state and runtime config.</p>
      </div>
      <div class="chips">
        {''.join(env_status)}
      </div>
    </div>

    <div class="grid">
      <div class="card" style="grid-column: 1 / -1;">
        <div class="section-title">
          <h2>Overview</h2>
          <div class="actions">
            <a class="btn secondary" href="/api/status?token={_esc(token)}">API status</a>
            <a class="btn secondary" href="/config?token={_esc(token)}">Config JSON</a>
            <a class="btn secondary" href="/logs?n=200&token={_esc(token)}">Logs</a>
          </div>
        </div>
        <div class="stats">{_render_status_cards(status)}</div>
      </div>

      <div class="card">
        <div class="section-title"><h2>Actions</h2></div>
        <div class="actions">
          <form method="post" action="/sync?token={_esc(token)}"><button type="submit">Sync commands</button></form>
          <form method="post" action="/reload?token={_esc(token)}"><input name="extension" placeholder="module name" /><button type="submit">Reload</button></form>
        </div>
        <p class="subtle">Use module names like <code>giveaway</code>, <code>gsay</code>, <code>signups</code>, <code>joinfamily</code>, <code>ocr_module</code>.</p>
      </div>

      <div class="card">
        <div class="section-title"><h2>Quick links</h2></div>
        <div class="actions">
          <a class="btn secondary" href="/settings?token={_esc(token)}">Module settings</a>
          <a class="btn secondary" href="/env?token={_esc(token)}">Environment</a>
          <a class="btn secondary" href="/music?token={_esc(token)}">Music</a>
          <a class="btn secondary" href="/logs?n=500&token={_esc(token)}">Last 500 log lines</a>
        </div>
        <div class="footer">Environment changes are written to <code>.env</code>; restart is required for most keys.</div>
      </div>

      <div class="card" style="grid-column: 1 / -1;">
        <div class="section-title"><h2>Live music sessions</h2></div>
        {players_html}
      </div>

      <div class="card">
        <div class="section-title"><h2>Web config</h2></div>
        <p class="subtle">Current <code>web_config.json</code> keys: <b>{_esc(", ".join(sorted(web_config.keys())) or "empty")}</b></p>
        <details>
          <summary>Preview</summary>
          <pre>{_esc(json.dumps(web_config, ensure_ascii=False, indent=2))}</pre>
        </details>
      </div>

      <div class="card">
        <div class="section-title"><h2>Environment</h2></div>
        <div class="field-grid">
          <div class="field"><label>Discord token</label><input type="password" readonly value="{'set' if current_env.get('DISCORD_TOKEN') else 'empty'}" /></div>
          <div class="field"><label>Owner</label><input readonly value="{_esc(current_env.get('OWNER_ID') or '')}" /></div>
          <div class="field"><label>Guild</label><input readonly value="{_esc(current_env.get('GUILD_ID') or '')}" /></div>
          <div class="field"><label>Lavalink</label><input readonly value="{_esc(current_env.get('LAVALINK_HOST') or '')}:{_esc(current_env.get('LAVALINK_PORT') or '')}" /></div>
        </div>
      </div>
    </div>
    """
    return _page("Musicbot Admin", token, body)


def _render_music_page(bot: discord.Client, token: str) -> str:
    status = _collect_runtime_status(bot)
    players = status.get("players") if isinstance(status.get("players"), list) else []
    body = f"""
    <div class="topbar">
      <div class="brand">
        <h1>Music</h1>
        <p>Live players, current track, queue, and control-message state.</p>
      </div>
      <div class="chips">
        <span class="badge green">{_esc(status.get("node_players") or 0)} players</span>
        <span class="badge">{_esc(status.get("guild_count") or 0)} guilds</span>
        <a class="btn secondary" href="/?token={_esc(token)}">Dashboard</a>
        <a class="btn secondary" href="/api/music?token={_esc(token)}">API</a>
      </div>
    </div>
    <div class="card">
      <div class="section-title"><h2>Summary</h2></div>
      <div class="stats">{_render_status_cards(status)}</div>
    </div>
    <div class="card" style="margin-top: 18px;">
      <div class="section-title"><h2>Players</h2></div>
      {_render_player_rows(players)}
    </div>
    """
    return _page("Music", token, body)


def _basic_auth_credentials() -> tuple[str, str]:
    return (
        os.getenv("WEB_ADMIN_BASIC_USER", "").strip(),
        os.getenv("WEB_ADMIN_BASIC_PASSWORD", "").strip(),
    )


def _require_token(request: web.Request) -> None:
    expected = os.getenv("WEB_ADMIN_TOKEN", "")
    basic_user, basic_password = _basic_auth_credentials()
    if not expected and not (basic_user and basic_password):
        raise web.HTTPUnauthorized(text="WEB_ADMIN_TOKEN is not set")

    provided = request.query.get("token")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    if expected and provided == expected:
        return

    if basic_user and basic_password:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="replace")
                username, password = decoded.split(":", 1)
            except (ValueError, binascii.Error):
                username, password = "", ""
            if username == basic_user and password == basic_password:
                return
        raise web.HTTPUnauthorized(
            text="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Musicbot Admin"'},
        )

    raise web.HTTPUnauthorized(text="Invalid token")


def _log_file_path() -> Path:
    log_dir = Path(os.getenv("MUSICBOT_LOG_DIR", "logs"))
    return log_dir / "bot.log"


def _log_file_candidates() -> list[Path]:
    candidates = [
        _log_file_path(),
        Path(os.getenv("MUSICBOT_LOG_FILE", "bot-runtime.log")),
        Path("bot-runtime.log"),
    ]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _read_log_tail_from_file(path: Path, lines_count: int) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return None, f"Failed to read log file {path}: {exc}"
    return "\n".join(lines[-lines_count:]) + "\n", str(path)


def _read_log_tail_from_journal(lines_count: int) -> tuple[str | None, str | None]:
    unit = os.getenv("WEB_ADMIN_JOURNAL_UNIT", "musicbot").strip() or "musicbot"
    try:
        completed = subprocess.run(
            ["journalctl", "-u", unit, "--no-pager", "-n", str(lines_count)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return None, f"Failed to query journalctl for {unit}: {exc}"

    output = (completed.stdout or completed.stderr or "").strip()
    if not output:
        return None, f"Journal for {unit} is empty or unavailable"
    return output + "\n", f"journalctl -u {unit}"


def _read_log_tail(lines_count: int) -> tuple[str, str]:
    errors: list[str] = []
    for candidate in _log_file_candidates():
        tail, source = _read_log_tail_from_file(candidate, lines_count)
        if tail is not None and source is not None:
            return tail, source
        if source:
            errors.append(source)

    journal_tail, source = _read_log_tail_from_journal(lines_count)
    if journal_tail is not None and source is not None:
        return journal_tail, source
    if source:
        errors.append(source)
    message = "\n".join(errors) if errors else "No log sources configured"
    return f"{message}\n", "unavailable"


async def _index(request: web.Request) -> web.Response:
    _require_token(request)
    bot = request.app["bot"]
    token = request.query.get("token", "")
    store: JsonStore = request.app["config_store"]
    return web.Response(text=_render_dashboard(bot, token, store), content_type="text/html")


async def _music(request: web.Request) -> web.Response:
    _require_token(request)
    bot = request.app["bot"]
    token = request.query.get("token", "")
    return web.Response(text=_render_music_page(bot, token), content_type="text/html")


async def _logs(request: web.Request) -> web.Response:
    _require_token(request)
    n = int(request.query.get("n", "200"))
    n = max(10, min(n, 5000))
    token = request.query.get("token", "")

    tail, source = _read_log_tail(n)
    body = f"""
    <div class="topbar">
      <div class="brand">
        <h1>Logs</h1>
        <p>Tail of <code>{_esc(source)}</code></p>
      </div>
      <div class="chips">
        <span class="badge">{_esc(n)} lines</span>
        <a class="btn secondary" href="/?token={_esc(token)}">Dashboard</a>
      </div>
    </div>
    <div class="card">
      <pre>{_esc(tail)}</pre>
    </div>
    """
    return web.Response(text=_page("Logs", body), content_type="text/html")


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


async def _api_status(request: web.Request) -> web.Response:
    _require_token(request)
    bot = request.app["bot"]
    store: JsonStore = request.app["config_store"]
    payload = {
        "runtime": _collect_runtime_status(bot),
        "web_config": store.load(),
        "env": _current_env_snapshot(),
        "log_file": str(_log_file_path()),
    }
    return web.json_response(payload, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, indent=2))


async def _api_music(request: web.Request) -> web.Response:
    _require_token(request)
    payload = _collect_runtime_status(request.app["bot"])
    return web.json_response(payload, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, indent=2))


async def _env_get(request: web.Request) -> web.Response:
    _require_token(request)
    token = request.query.get("token", "")
    current = _current_env_snapshot()
    body = f"""
    <div class="topbar">
      <div class="brand">
        <h1>Environment</h1>
        <p>Edit <code>.env</code> values for the running bot. Restart required for most keys.</p>
      </div>
      <div class="chips">
        <span class="badge yellow">Restart required</span>
        <span class="badge">File: {_esc(_env_file_path())}</span>
      </div>
    </div>
    <div class="card">
      <div class="section-title">
        <h2>Editable parameters</h2>
        <div class="actions">
          <a class="btn secondary" href="/?token={_esc(token)}">Dashboard</a>
          <a class="btn secondary" href="/config?token={_esc(token)}">JSON config</a>
        </div>
      </div>
      <form method="post" action="/env/save?token={_esc(token)}">
        <div class="field-grid">
          {_render_env_sections()}
        </div>
        <div class="actions" style="margin-top: 16px;">
          <button type="submit" class="btn success">Save .env</button>
        </div>
      </form>
    </div>
    <div class="card" style="margin-top: 18px;">
      <div class="section-title"><h2>Current snapshot</h2></div>
      <details open>
        <summary>Show current values</summary>
        <pre>{_esc(json.dumps(current, ensure_ascii=False, indent=2))}</pre>
      </details>
    </div>
    """
    return web.Response(text=_page("Environment", token, body), content_type="text/html")


async def _env_save(request: web.Request) -> web.Response:
    _require_token(request)
    token = request.query.get("token", "")
    current = _current_env_snapshot()
    data = await request.post()
    updates: dict[str, str] = {}
    for key, *_ in ENV_EDITABLE_FIELDS:
        if key not in data:
            continue
        raw_value = str(data.get(key, "")).strip()
        if not raw_value and key in current and current[key] is not None:
            continue
        updates[key] = raw_value

    if updates:
        _write_env_file(updates)

    raise web.HTTPFound(location=f"/env?token={quote_plus(token)}")

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

    body = f"""
    <div class="topbar">
      <div class="brand">
        <h1>Module settings</h1>
        <p>Edit <code>web_config.json</code> for module-specific overrides.</p>
      </div>
      <div class="chips">
        <span class="badge yellow">Reload extension after save</span>
        <span class="badge">{_esc(len(cfg))} top-level keys</span>
      </div>
    </div>

    <div class="card">
      <div class="section-title">
        <h2>Modules</h2>
        <div class="actions">
          <a class="btn secondary" href="/?token={_esc(token)}">Dashboard</a>
          <a class="btn secondary" href="/env?token={_esc(token)}">Environment</a>
        </div>
      </div>
      <form method="post" action="/settings/save?token={_esc(token)}">
        <div class="field-grid">
          <div class="field">
            <label>Giveaway admin role ID</label>
            {_field_input("giveaway_admin_role_id", str(giveaway.get("admin_role_id") or ""), placeholder="1234567890")}
            <div class="hint">Controls giveaway admin checks.</div>
          </div>
          <div class="field">
            <label>GSay allowed roles</label>
            {_field_input("gsay_allowed_roles", gsay_allowed_roles_value, placeholder="1,2,3")}
            <div class="hint">Comma-separated role IDs allowed to use /gsay.</div>
          </div>
          <div class="field">
            <label>JoinFamily HR access</label>
            {_field_input("joinfamily_hr_access", joinfamily_hr_access_value, placeholder="1,2,3")}
            <div class="hint">Comma-separated role IDs with HR access.</div>
          </div>
          <div class="field">
            <label>JoinFamily log channel</label>
            {_field_input("joinfamily_log_channel_id", str(joinfamily.get("log_channel_id") or ""), placeholder="1234567890")}
          </div>
          <div class="field">
            <label>JoinFamily remove role</label>
            {_field_input("joinfamily_remove_role_id", str(joinfamily.get("remove_role_id") or ""), placeholder="1234567890")}
          </div>
          <div class="field">
            <label>JoinFamily add role 1</label>
            {_field_input("joinfamily_add_role_1_id", str(joinfamily.get("add_role_1_id") or ""), placeholder="1234567890")}
          </div>
          <div class="field">
            <label>JoinFamily add role 2</label>
            {_field_input("joinfamily_add_role_2_id", str(joinfamily.get("add_role_2_id") or ""), placeholder="1234567890")}
          </div>
          <div class="field">
            <label>Signups managers</label>
            {_field_input("signups_managers", signups_managers_value, placeholder="1,2,3")}
          </div>
          <div class="field">
            <label>Signups admins</label>
            {_field_input("signups_admins", signups_admins_value, placeholder="1,2,3")}
          </div>
        </div>
        <div class="actions" style="margin-top: 16px;">
          <button type="submit" class="btn success">Save module config</button>
        </div>
      </form>
    </div>

    <div class="card" style="margin-top: 18px;">
      <div class="section-title"><h2>Raw web_config.json</h2></div>
      <pre>{_esc(json.dumps(cfg, ensure_ascii=False, indent=2))}</pre>
    </div>
    """
    return web.Response(text=_page("Module settings", body), content_type="text/html")


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

    host = os.getenv("WEB_ADMIN_HOST", "0.0.0.0")
    port_raw = os.getenv("WEB_ADMIN_PORT", "8080").strip()
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning("Invalid WEB_ADMIN_PORT=%s, falling back to 8080", port_raw)
        port = 8080

    store = JsonStore(data_path("web_config.json"))

    app = web.Application()
    app["bot"] = bot
    app["config_store"] = store

    app.add_routes(
        [
            web.get("/", _index),
            web.get("/music", _music),
            web.get("/logs", _logs),
            web.get("/config", _config_get),
            web.post("/config", _config_post),
            web.get("/api/status", _api_status),
            web.get("/api/music", _api_music),
            web.get("/env", _env_get),
            web.post("/env/save", _env_save),
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
