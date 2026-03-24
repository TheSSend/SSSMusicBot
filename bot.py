import discord
import wavelink
import asyncio
import os
import sys
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from music_core import MusicPlayer, start_track, send_control_message
from edit_guard import safe_message_edit, start_cleanup_task

logger = logging.getLogger(__name__)

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
                    name=f"{player.current_track.title} — {player.current_track.author}"
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

# ================= SETUP =================

async def setup_hook():

    logger.info("setup_hook: начало выполнения")

    for extension in (
        "giveaway",
        "gsay",
        "ocr_module",
        "signups",
        "joinfamily",
        "forum_search",
    ):
        try:
            await bot.load_extension(extension)
            logger.info("Загружен модуль %s", extension)
        except Exception:
            logger.exception("Ошибка загрузки модуля %s", extension)

    # ================= SYNC =================

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
        logger.error(f"Ошибка синхронизации: {e}")

    # ================= LAVALINK =================

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
        logger.error(f"Lavalink ошибка: {e}")

    # Start cleanup task for edit_guard
    start_cleanup_task()

bot.setup_hook = setup_hook

# ================= NODE EVENTS =================

@bot.event
async def on_wavelink_node_ready(payload):

    logger.info("Lavalink node ready: %s", payload.node.identifier)
    await update_presence()

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

        next_track = await player.queue.get_wait()

        await start_track(player, next_track, False)
        await update_presence(player)
        return

    if player.control_message:
        await safe_message_edit(player.control_message, view=None)

    player.current_track = None
    player.track_start_time = None

    await update_presence(None)

# ================= PLAYER DESTROY =================

@bot.listen("on_wavelink_player_destroy")
async def on_player_destroy(payload):

    await update_presence(None)

# ================= PLAY COMMAND =================

@bot.tree.command(
    name="play",
    description="🎵 Воспроизвести трек или добавить в очередь"
)
@app_commands.describe(query="Название трека или ссылка")

async def play(interaction: discord.Interaction, query: str):

    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ Ты не в голосовом канале",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    await play_music(interaction, query)

# ================= PLAY LOGIC =================

async def play_music(interaction: discord.Interaction, query: str):

    try:
        node = wavelink.Pool.get_node()

        if not node:
            await interaction.followup.send(
                "❌ Lavalink не подключен.",
                ephemeral=True
            )
            return

        # ================= CONNECT =================

        channel = interaction.user.voice.channel

        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.followup.send(
                "❌ Не удалось получить данные бота на сервере.",
                ephemeral=True
            )
            return

        permissions = channel.permissions_for(bot_member)

        if not permissions.connect:
            await interaction.followup.send(
                "❌ У бота нет права подключаться к этому голосовому каналу.",
                ephemeral=True
            )
            return

        if not permissions.speak:
            await interaction.followup.send(
                "❌ У бота нет права говорить в этом голосовом канале.",
                ephemeral=True
            )
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
                player = await channel.connect(
                    cls=MusicPlayer,
                    self_deaf=True,
                    timeout=60.0,
                    reconnect=False
                )
            except wavelink.ChannelTimeoutException:
                logger.warning("Таймаут подключения к голосовому каналу, пробую повторно")

                stale_client = interaction.guild.voice_client
                if stale_client is not None:
                    try:
                        await stale_client.disconnect(force=True)
                    except Exception:
                        logger.exception("Не удалось отключить stale voice client после таймаута")

                await asyncio.sleep(2)

                player = await channel.connect(
                    cls=MusicPlayer,
                    self_deaf=True,
                    timeout=60.0,
                    reconnect=False
                )
            except Exception:
                logger.exception("Не удалось подключить плеер к голосовому каналу %s", channel.id)
                await interaction.followup.send(
                    "❌ Не удалось подключиться к голосовому каналу. Проверь права `Подключаться`/`Говорить` и попробуй снова.",
                    ephemeral=True
                )
                return

            await asyncio.sleep(1)

        if player is None:
            await interaction.followup.send(
                "❌ Не удалось подключиться к голосовому каналу.",
                ephemeral=True
            )
            return

        # ================= SEARCH =================

        results = await wavelink.Playable.search(query)

        if not results:
            await interaction.followup.send("❌ Ничего не найдено")
            return

        if isinstance(results, wavelink.Playlist):
            track = results.tracks[0]
        else:
            track = results[0]

        track.requester = interaction.user

        # ================= PLAY =================

        if not player.playing and not player.paused:

            if not player.control_message:
                await send_control_message(interaction, player)

            await asyncio.sleep(0.5)

            await start_track(player, track, False)

            await update_presence(player)

            await interaction.followup.send(
                f"🎵 Сейчас играет: **{track.title}**"
            )

        else:

            await player.queue.put_wait(track)

            await interaction.followup.send(
                f"🎵 Добавлено в очередь: **{track.title}**"
            )

    except Exception as e:
        logger.exception("Ошибка при выполнении команды play")
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

# ================= RELOAD =================

@bot.tree.command(
    name="reload",
    description="Перезагрузить модуль"
)
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
        logger.exception("Ошибка при reload extension %s", extension)
        await interaction.followup.send(f"❌ {e}", ephemeral=True)

# ================= SYNC =================

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

# ================= READY =================

@bot.event
async def on_ready():

    logger.info("Бот %s готов", bot.user)
    logger.info("Команды: %s", [c.name for c in bot.tree.get_commands()])
    if GUILD_ID is not None:
        logger.info("Guild sync target: %s", GUILD_ID)
        logger.info("Guild commands: %s", [c.name for c in bot.tree.get_commands(guild=discord.Object(id=GUILD_ID))])

    await update_presence()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):

    if isinstance(error, app_commands.CommandNotFound):
        logger.error(
            "CommandNotFound name=%s guild=%s available_global=%s available_guild=%s",
            error.name,
            getattr(interaction.guild, "id", None),
            [c.name for c in bot.tree.get_commands()],
            [c.name for c in bot.tree.get_commands(guild=interaction.guild)] if interaction.guild else [],
        )
        return

    logger.exception("Ошибка app command: %s", error)
       
# ================= RUN =================

if __name__ == "__main__":
    bot.run(TOKEN)
