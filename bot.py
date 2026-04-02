import discord
import wavelink
import asyncio
import os
import sys
import logging
import time
import base64
from pathlib import Path
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from music_core import (
    MusicPlayer, start_track, send_control_message, build_embed,
    build_queue_preview, MusicControls, display_author, send_temporary_followup,
    get_music_controls,
    dump_player_state
)
from edit_guard import safe_message_edit, start_cleanup_task
from json_store import JsonStore
from runtime_paths import data_path
from web_admin import maybe_start_web_admin

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

_music_update_task: asyncio.Task | None = None
_idle_disconnect_tasks: dict[int, asyncio.Task] = {}
_last_state_dump: dict[int, float] = {}
STATE_DUMP_INTERVAL_SECONDS = int(os.getenv("PLAYER_STATE_DUMP_INTERVAL", "15"))
_web_admin_runner = None

# Store for player resumes
state_store = JsonStore(data_path("player_state.json"))


async def restore_control_message(guild: discord.Guild, player: MusicPlayer, pd: dict) -> None:
    text_channel_id = pd.get("text_channel_id", 0)
    if not text_channel_id:
        return

    text_channel = guild.get_channel(text_channel_id)
    if not isinstance(text_channel, discord.TextChannel):
        return

    control_message_id = pd.get("control_message_id", 0)
    if control_message_id:
        try:
            message = await text_channel.fetch_message(control_message_id)
            view = get_music_controls(player)
            bot.add_view(view, message_id=message.id)
            await safe_message_edit(
                message,
                embed=build_embed(player),
                view=view,
            )
            player.control_message = message
            logger.info("Restored existing control message guild=%s message=%s", guild.id, control_message_id)
            return
        except discord.NotFound:
            logger.info("Control message was deleted, creating a new one guild=%s message=%s", guild.id, control_message_id)
        except Exception:
            logger.exception("Failed to fetch existing control message guild=%s message=%s", guild.id, control_message_id)

    try:
        view = get_music_controls(player)
        message = await text_channel.send(
            embed=build_embed(player),
            view=view,
        )
        bot.add_view(view, message_id=message.id)
        player.control_message = message
        logger.info("Created new control message guild=%s channel=%s", guild.id, text_channel.id)
    except Exception:
        logger.exception("Failed to create control message for resumed player guild=%s", guild.id)

# ================= LOGGING =================

log_dir_value = os.getenv("MUSICBOT_LOG_DIR", "logs")
LOG_DIR = Path(log_dir_value)

handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    sys.stderr.write(f"Warning: cannot create log directory '{LOG_DIR}': {exc}\n")
else:
    handlers.append(
        RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    handlers=handlers,
    force=True,
)

logger.info("BOT STARTED")
logger.info(f"Python version: {sys.version}")

if sys.platform != "win32":
    try:
        import uvloop
    except ImportError:
        logger.info("uvloop не установлен, используется стандартный event loop")
    else:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logger.info("uvloop включен")

# ================= ENV =================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is not set")

owner_id_str = os.getenv("OWNER_ID")
if not owner_id_str:
    raise ValueError("OWNER_ID environment variable is not set")
OWNER_ID = int(owner_id_str)

LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
lavalink_port_str = os.getenv("LAVALINK_PORT", "2333")
try:
    LAVALINK_PORT = int(lavalink_port_str)
except ValueError:
    LAVALINK_PORT = 2333
    logging.warning(f"Invalid LAVALINK_PORT value: {lavalink_port_str}, using default 2333")

LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
guild_id_str = os.getenv("GUILD_ID")
GUILD_ID = int(guild_id_str) if guild_id_str and guild_id_str.isdigit() else None

# ================= DISCORD BOT =================

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= PRESENCE =================

async def update_presence(player=None):
    if bot.ws is None:
        return

    if player and player.current_track:
        try:
            await bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=f"{player.current_track.title} — {display_author(player.current_track.author)}"
                )
            )
        except Exception:
            logger.exception("Не удалось обновить presence для текущего трека")
    else:
        try:
            await bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="Следит за сервером 👀"
                )
            )
        except Exception:
            logger.exception("Не удалось обновить стандартный presence")

def cancel_idle_disconnect(player_or_guild_id):
    guild_id = player_or_guild_id
    if hasattr(player_or_guild_id, "guild") and getattr(player_or_guild_id.guild, "id", None):
        guild_id = player_or_guild_id.guild.id

    task = _idle_disconnect_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()

def schedule_idle_disconnect(player: MusicPlayer, delay: int = 10):
    guild_id = getattr(player.guild, "id", None)
    if guild_id is None:
        return

    cancel_idle_disconnect(guild_id)

    async def _runner():
        try:
            await asyncio.sleep(delay)
            voice_client = getattr(player.guild, "voice_client", None)
            if voice_client is player and player.queue.is_empty and not player.playing and not player.paused:
                try:
                    control_message = getattr(player, "control_message", None)
                    if control_message is not None:
                        try:
                            await control_message.delete()
                        except discord.NotFound:
                            pass
                        except Exception:
                            logger.exception("Failed to delete control message for guild=%s", guild_id)
                        finally:
                            player.control_message = None

                    await player.disconnect(force=True)
                except Exception:
                    logger.exception("Failed to disconnect idle player for guild=%s", guild_id)
        except asyncio.CancelledError:
            return
        finally:
            current = _idle_disconnect_tasks.get(guild_id)
            if current is asyncio.current_task():
                _idle_disconnect_tasks.pop(guild_id, None)

    _idle_disconnect_tasks[guild_id] = asyncio.create_task(_runner())

async def music_controls_updater():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            node = wavelink.Pool.get_node()
            for player in node.players.values():
                if not player or not getattr(player, "current_track", None):
                    continue

                # Auto-resume state dumping (throttled to reduce disk I/O)
                try:
                    guild_id = getattr(getattr(player, "guild", None), "id", None)
                    now = time.time()
                    last = _last_state_dump.get(guild_id or 0, 0.0)
                    if guild_id and (now - last) >= STATE_DUMP_INTERVAL_SECONDS:
                        dump_player_state(player, state_store)
                        _last_state_dump[guild_id] = now
                except Exception:
                    logger.exception("Failed to dump player state")

                if not getattr(player, "control_message", None):
                    guild_id = getattr(getattr(player, "guild", None), "id", None)
                    if not guild_id:
                        continue
                    state = state_store.load()
                    pd = state.get(str(guild_id), {})
                    if pd:
                        try:
                            await restore_control_message(player.guild, player, pd)
                        except Exception:
                            logger.exception("Failed to restore control message during update guild=%s", guild_id)
                    if not getattr(player, "control_message", None):
                        continue

                try:
                    await safe_message_edit(
                        player.control_message,
                        embed=build_embed(player),
                        view=get_music_controls(player),
                    )
                except Exception:
                    logger.exception("Не удалось обновить контролы музыки для guild=%s", getattr(player.guild, "id", None))
        except Exception:
            pass

        await asyncio.sleep(3)

# ================= SETUP =================

async def setup_hook():
    logger.info("setup_hook: начало выполнения")

    for extension in (
        "giveaway",
        "gsay",
        "ocr_module",
        "signups",
        "joinfamily",
    ):
        try:
            await bot.load_extension(extension)
            logger.info("Загружен модуль %s", extension)
        except Exception:
            logger.exception("Ошибка загрузки модуля %s", extension)

    try:
        if GUILD_ID is not None:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info("Команды синхронизированы для guild: %s", [cmd.name for cmd in synced])
        else:
            synced = await bot.tree.sync()
            logger.info("Команды синхронизированы глобально: %s", [cmd.name for cmd in synced])
    except Exception as e:
        logger.error("Ошибка синхронизации: %s", e)

    logger.info("Подключение к Lavalink...")
    node = wavelink.Node(
        uri=f"http://{LAVALINK_HOST}:{LAVALINK_PORT}",
        password=LAVALINK_PASSWORD
    )

    try:
        await wavelink.Pool.connect(
            client=bot,
            nodes=[node]
        )
        logger.info("Lavalink подключен")
    except Exception as e:
        logger.error("Lavalink ошибка: %s", e)

    start_cleanup_task()

    global _music_update_task
    if _music_update_task is None or _music_update_task.done():
        _music_update_task = asyncio.create_task(music_controls_updater())

    global _web_admin_runner
    if _web_admin_runner is None:
        try:
            _web_admin_runner = await maybe_start_web_admin(bot)
        except Exception:
            logger.exception("Failed to start web admin")

bot.setup_hook = setup_hook

# ================= NODE EVENTS =================

@bot.event
async def on_wavelink_node_ready(payload):
    logger.info("Lavalink node ready: %s", payload.node.identifier)
    await update_presence()

    # ---- Auto Resume Implementation ----
    state = state_store.load()
    to_delete = []

    for guild_id_str, pd in state.items():
        guild = bot.get_guild(int(guild_id_str))
        if not guild:
            continue
            
        voice_channel = guild.get_channel(pd.get("channel_id"))
        if not voice_channel:
            to_delete.append(guild_id_str)
            continue

        try:
            player = await voice_channel.connect(cls=MusicPlayer)
            
            # Restore current track
            current_encoded = pd.get("track_encoded")
            if current_encoded:
                tracks = await wavelink.Playable.search(current_encoded)
                if tracks:
                    track = tracks[0] if isinstance(tracks, list) else tracks
                    await player.play(track)
                    
                    pos = pd.get("position", 0)
                    if pos > 0:
                        await player.seek(pos)
                        player.track_start_time = time.time() - (pos / 1000)

            # Restore queue
            for q_encoded in pd.get("queue_encoded", []):
                q_tracks = await wavelink.Playable.search(q_encoded)
                if q_tracks:
                    q_track = q_tracks[0] if isinstance(q_tracks, list) else q_tracks
                    player.queue.put(q_track)

            # Restore control message
            await restore_control_message(guild, player, pd)

        except Exception:
            logger.exception("Failed to auto-resume player for guild %s", guild_id_str)
            to_delete.append(guild_id_str)

    # Cleanup invalid states
    if to_delete:
        for gd in to_delete:
            state.pop(gd, None)
        state_store.save(state)

@bot.event
async def on_wavelink_node_disconnected(payload):
    logger.warning("Lavalink node disconnected: %s", payload.reason)

# ================= TRACK END =================

@bot.listen("on_wavelink_track_end")
async def on_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    reason = payload.reason

    logger.info("Track ended: %s", reason)

    if not player:
        return
    if reason == "REPLACED":
        return

    if not player.queue.is_empty:
        cancel_idle_disconnect(player)
        next_track = await player.queue.get_wait()
        await start_track(player, next_track, False)
        await update_presence(player)
        return

    if player.control_message:
        await safe_message_edit(player.control_message, view=None)

    player.current_track = None
    player.track_start_time = None

    # Clear state when queue ends
    state = state_store.load()
    if str(player.guild.id) in state:
        state.pop(str(player.guild.id), None)
        state_store.save(state)

    await update_presence(None)
    schedule_idle_disconnect(player, delay=10)

@bot.listen("on_wavelink_player_destroy")
async def on_player_destroy(payload):
    await update_presence(None)

@bot.event
async def on_music_stopped():
    await update_presence(None)

# ================= PLAY COMMAND =================

@bot.tree.command(
    name="play",
    description="🎵 Воспроизвести трек, плейлист или ссылку"
)
@app_commands.describe(query="Название трека, ссылка на YouTube/Spotify")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ Ты не в голосовом канале", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    await play_music(interaction, query)

# ================= PLAY LOGIC =================

async def play_music(interaction: discord.Interaction, query: str):
    try:
        node = wavelink.Pool.get_node()
        if not node:
            await interaction.followup.send("❌ Lavalink не подключен.", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        bot_member = interaction.guild.me

        if bot_member is None:
            await interaction.followup.send("❌ Не удалось получить данные бота на сервере.", ephemeral=True)
            return

        permissions = channel.permissions_for(bot_member)
        if not permissions.connect:
            await interaction.followup.send("❌ У бота нет права подключаться к этому голосовому каналу.", ephemeral=True)
            return
        if not permissions.speak:
            await interaction.followup.send("❌ У бота нет права говорить в этом голосовом канале.", ephemeral=True)
            return

        voice_client = interaction.guild.voice_client
        player: MusicPlayer | None = None

        if isinstance(voice_client, MusicPlayer):
            player = voice_client
        elif voice_client is not None:
            logger.warning("Найден не-MusicPlayer voice client, отключаю и пробую заново")
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                logger.exception("Не удалось отключить старый voice client")

        if player:
            if player.channel != channel:
                logger.info("Перемещаю плеер в другой голосовой канал")
                await player.move_to(channel)
        else:
            try:
                player = await channel.connect(cls=MusicPlayer, self_deaf=True, timeout=60.0, reconnect=False)
            except wavelink.ChannelTimeoutException:
                stale_client = interaction.guild.voice_client
                if stale_client is not None:
                    try:
                        await stale_client.disconnect(force=True)
                    except Exception:
                        pass
                await asyncio.sleep(2)
                player = await channel.connect(cls=MusicPlayer, self_deaf=True, timeout=60.0, reconnect=False)
            except Exception:
                await interaction.followup.send("❌ Не удалось подключиться к голосовому каналу.", ephemeral=True)
                return

            await asyncio.sleep(1)

        if player is None:
            await interaction.followup.send("❌ Не удалось подключиться.", ephemeral=True)
            return

        normalized = query.strip()
        try:
            results = await fetch_best_tracks(node, normalized)
        except wavelink.LavalinkLoadException as exc:
            await interaction.followup.send("❌ Не удалось загрузить треки.", ephemeral=True)
            return
        except wavelink.LavalinkException:
            await interaction.followup.send("❌ Ошибка поиска на стороне Lavalink.", ephemeral=True)
            return

        if not results:
            await interaction.followup.send("❌ Ничего не найдено")
            return

        # ---- PLAYLIST SUPPORT (NEW) ----
        tracks = []
        is_playlist = False
        playlist_name = "Плейлист"

        if isinstance(results, wavelink.Playlist):
            tracks = results.tracks
            is_playlist = True
            playlist_name = getattr(results, "name", "Плейлист")
        else:
            tracks = [results[0]]

        for t in tracks:
            t.requester = interaction.user

        first_track = tracks[0]

        if not player.playing and not player.paused:
            cancel_idle_disconnect(player)

            if not player.control_message:
                await send_control_message(interaction, player)

            await asyncio.sleep(0.5)

            await start_track(player, first_track, False)
            await update_presence(player)

            if is_playlist:
                for t in tracks[1:]:
                    await player.queue.put_wait(t)
                await send_temporary_followup(
                    interaction,
                    content=f"🎵 Сейчас играет: **{first_track.title}**\n📥 Добавлен плейлист: **{playlist_name}** ({len(tracks)} треков)",
                    delete_after=10,
                )
            else:
                await send_temporary_followup(
                    interaction,
                    content=f"🎵 Сейчас играет: **{first_track.title}**",
                    delete_after=5,
                )
        else:
            cancel_idle_disconnect(player)

            if is_playlist:
                for t in tracks:
                    await player.queue.put_wait(t)
                await send_temporary_followup(
                    interaction,
                    content=f"📥 Плейлист добавлен в очередь: **{playlist_name}** ({len(tracks)} треков)",
                    delete_after=10,
                )
            else:
                await player.queue.put_wait(first_track)
                await send_temporary_followup(
                    interaction,
                    content=f"🎵 Добавлено в очередь: **{first_track.title}**",
                    delete_after=5,
                )

    except Exception as e:
        logger.exception("Ошибка при выполнении команды play")
        await interaction.followup.send(f"❌ Ошибка", ephemeral=True)

def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in YOUTUBE_HOSTS

def sanitize_search_text(value: str) -> str:
    cleaned = value.replace("-", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()

def normalize_author(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = sanitize_search_text(value)
    if cleaned.endswith(" Topic"):
        cleaned = cleaned[:-6].strip()
    return cleaned or None

def normalize_query(query: str) -> str:
    return query.strip()

async def build_search_candidates(query: str) -> list[str]:
    normalized = normalize_query(query)
    if not normalized:
        return []
    candidates: list[str] = []
    if is_youtube_url(normalized):
        return candidates
    sanitized = sanitize_search_text(normalized)
    candidates.append(f"ytsearch:{normalized}")
    if sanitized != normalized:
        candidates.append(f"ytsearch:{sanitized}")
    candidates.append(f"scsearch:{normalized}")
    return candidates

def build_metadata_candidates(title: str | None, author: str | None) -> list[str]:
    candidates: list[str] = []
    title_clean = sanitize_search_text(title or "") or None
    author_clean = normalize_author(author)
    combos = []
    if title_clean and author_clean:
        combos.append(f"{title_clean} {author_clean}")
    if title_clean:
        combos.append(title_clean)

    seen = set()
    for combined in combos:
        normalized = sanitize_search_text(combined)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(f"ytsearch:{normalized}")
        candidates.append(f"scsearch:{normalized}")
    return candidates

async def resolve_with_ytdlp(query: str) -> tuple[str | None, str | None]:
    try:
        import yt_dlp
    except ImportError:
        return None, None
    normalized = normalize_query(query)
    if not normalized:
        return None, None
    target = normalized if is_youtube_url(normalized) else f"ytsearch1:{normalized}"
    options = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "extract_flat": False,
        "format": "bestaudio[protocol^=http]/bestaudio/best",
    }
    def _extract():
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=False)
        if not info: return None, None
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if not entries: return None, None
            info = entries[0]
        title = str(info.get("title") or "").strip() or None
        author = str(info.get("uploader") or info.get("channel") or info.get("artist") or "").strip() or None
        return title, author
    try:
        return await asyncio.to_thread(_extract)
    except Exception:
        return None, None

def apply_track_metadata(track, *, title: str | None = None, author: str | None = None):
    if title:
        try: track._title = title
        except Exception: pass
    normalized_author = normalize_author(author)
    if normalized_author:
        try: track._author = normalized_author
        except Exception: pass

async def fetch_best_tracks(node: wavelink.Node, query: str):
    candidates = await build_search_candidates(query)
    last_exc = None

    for candidate in candidates:
        try:
            results = await wavelink.Pool.fetch_tracks(candidate, node=node)
            if results: return results
        except wavelink.LavalinkException as exc:
            last_exc = exc
            continue

    title, author = await resolve_with_ytdlp(query)
    metadata_candidates = build_metadata_candidates(title, author)

    for candidate in metadata_candidates:
        try:
            results = await wavelink.Pool.fetch_tracks(candidate, node=node)
            if results: return results
        except wavelink.LavalinkException:
            continue

    if last_exc is not None:
        raise last_exc
    return []


async def get_active_music_player(interaction: discord.Interaction) -> MusicPlayer | None:
    voice_client = getattr(interaction.guild, "voice_client", None)
    if not isinstance(voice_client, MusicPlayer):
        await interaction.response.send_message("❌ Музыка сейчас не играет.", ephemeral=True)
        return None
    return voice_client


@bot.tree.command(name="np", description="Показать текущий трек")
async def now_playing(interaction: discord.Interaction):
    player = await get_active_music_player(interaction)
    if not player:
        return

    await interaction.response.send_message(embed=build_embed(player), ephemeral=True)


@bot.tree.command(name="queue", description="Показать очередь")
async def show_queue(interaction: discord.Interaction):
    player = await get_active_music_player(interaction)
    if not player:
        return

    if player.queue.is_empty:
        await interaction.response.send_message("Очередь пуста.", ephemeral=True)
        return

    current = player.current_track.title if player.current_track else "Нет трека"
    text = build_queue_preview(player)
    await interaction.response.send_message(
        f"Сейчас играет: **{current}**\n\n{text}",
        ephemeral=True,
    )

@bot.tree.command(name="reload", description="Перезагрузить модуль")
async def reload_extension(interaction: discord.Interaction, extension: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.reload_extension(extension)
        if GUILD_ID is not None:
            await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        else:
            await bot.tree.sync()
        await interaction.followup.send("✅ Перезагружено", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)

@bot.tree.command(name="sync")
async def sync(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Нет доступа", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if GUILD_ID is not None:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    else:
        await bot.tree.sync()
    await interaction.followup.send("✅ Синхронизировано", ephemeral=True)

@bot.event
async def on_ready():
    logger.info("Бот %s готов", bot.user)
    await update_presence()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        return
    logger.exception("Ошибка app command: %s", error)
       
if __name__ == "__main__":
    bot.run(TOKEN)
