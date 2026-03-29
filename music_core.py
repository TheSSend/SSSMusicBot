import discord
import wavelink
import asyncio
import time
import logging

from wavelink import LavalinkException

from edit_guard import safe_message_edit

logger = logging.getLogger(__name__)


def display_author(value: str | None) -> str:

    if not value:
        return "Неизвестный исполнитель"

    normalized = " ".join(value.split()).strip()

    for suffix in (" - Topic", " – Topic", " — Topic"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].strip()
            break

    return normalized or "Неизвестный исполнитель"


def display_requester(value) -> str:

    if value is None:
        return "Неизвестно"

    mention = getattr(value, "mention", None)
    if mention:
        return mention

    name = getattr(value, "display_name", None) or getattr(value, "name", None) or str(value)
    return " ".join(str(name).split()).strip() or "Неизвестно"

# ================= PLAYER =================

class MusicPlayer(wavelink.Player):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.queue = wavelink.Queue()
        self.control_message: discord.Message | None = None
        self.current_track: wavelink.Playable | None = None
        self.track_start_time: float | None = None

    async def on_voice_state_update(self, data):
        await super().on_voice_state_update(data)

        if data.get("channel_id"):
            try:
                await self._dispatch_voice_update()
            except Exception:
                logger.exception("Ошибка dispatch VOICE_UPDATE после voice state update")

    async def on_voice_server_update(self, data):
        try:
            await super().on_voice_server_update(data)
        except Exception:
            logger.exception("Ошибка при обработке voice server update")
            raise

    async def _dispatch_voice_update(self):
        voice = self._voice_state.get("voice", {})

        session_id = voice.get("session_id")
        token = voice.get("token")
        endpoint = voice.get("endpoint")

        if not session_id or not token or not endpoint:
            return

        request = {
            "voice": {
                "sessionId": session_id,
                "token": token,
                "endpoint": endpoint,
                "channelId": str(getattr(getattr(self, "channel", None), "id", "")),
            }
        }

        try:
            await self.node._update_player(self.guild.id, data=request)
        except LavalinkException as exc:
            logger.error(
                "Lavalink rejected voice update guild=%s status=%s error=%s path=%s trace=%s",
                getattr(self.guild, "id", None),
                exc.status,
                exc.error,
                exc.path,
                exc.trace,
            )
            await self.disconnect()
        except Exception:
            logger.exception("Unexpected error while dispatching voice update")
            raise
        else:
            self._connection_event.set()


# ================= PROGRESS BAR =================

def progress_bar(elapsed, total):

    if total <= 0:
        return "░" * 10

    ratio = elapsed / total
    filled = int(ratio * 10)

    if filled > 10:
        filled = 10

    return "▰" * filled + "▱" * (10 - filled)


# ================= EMBED =================

def build_embed(player: MusicPlayer):

    track = player.current_track

    if not track:
        return discord.Embed(
            title="🎵 Музыка",
            description="Ничего не играет",
            color=0x2ecc71
        )

    # Handle case where track_start_time might be None
    if player.track_start_time is None:
        elapsed = 0
    else:
        elapsed = int(time.time() - player.track_start_time)
        
    total = int(track.length / 1000)

    if elapsed > total:
        elapsed = total

    bar = progress_bar(elapsed, total)

    embed = discord.Embed(
        title="🎵 Сейчас играет",
        color=0x57F287,
    )

    embed.add_field(name="Трек", value=f"**{track.title}**", inline=False)

    embed.add_field(
        name="Исполнитель",
        value=display_author(track.author),
        inline=True,
    )

    requester = getattr(track, "requester", None)
    embed.add_field(
        name="Включил",
        value=display_requester(requester),
        inline=True,
    )

    embed.add_field(
        name="Прогресс",
        value=f"`{elapsed//60:02}:{elapsed%60:02}` {bar} `{total//60:02}:{total%60:02}`",
        inline=False,
    )

    artwork = getattr(track, "artwork", None)
    if artwork:
        embed.set_thumbnail(url=artwork)

    return embed


# ================= CONTROLS =================

class MusicControls(discord.ui.View):

    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="Пауза", emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def pause(self, interaction: discord.Interaction, _):

        if not self.player:
            return

        await interaction.response.defer()

        try:
            await self.player.pause(not self.player.paused)
        except LavalinkException:
            logger.warning("Pause failed because Lavalink lost the player; disconnecting locally")
            try:
                await self.player.disconnect()
            except Exception:
                logger.exception("Failed to disconnect player after pause error")
        except Exception:
            logger.exception("Unexpected error while toggling pause")

    @discord.ui.button(label="Следующий", emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def skip(self, interaction: discord.Interaction, _):

        await interaction.response.defer()

        if self.player:
            try:
                await self.player.stop()
            except LavalinkException:
                logger.warning("Skip failed because Lavalink lost the player; disconnecting locally")
                try:
                    await self.player.disconnect()
                except Exception:
                    logger.exception("Failed to disconnect player after skip error")
            except Exception:
                logger.exception("Unexpected error while skipping track")

    @discord.ui.button(label="Очередь", emoji="📜", style=discord.ButtonStyle.success, row=1)
    async def queue(self, interaction: discord.Interaction, _):

        await interaction.response.defer(ephemeral=True)

        if self.player.queue.is_empty:
            await interaction.followup.send("📭 Очередь пуста", ephemeral=True)
            return

        text = "\n".join(
            f"{i+1}. {t.title}"
            for i, t in enumerate(list(self.player.queue)[:10])
        )

        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Стоп", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop(self, interaction: discord.Interaction, _):

        await interaction.response.defer()

        if self.player:
            try:
                await self.player.disconnect()
            except LavalinkException:
                logger.warning("Stop failed because Lavalink lost the player; forcing local cleanup")
            except Exception:
                logger.exception("Unexpected error while stopping player")


# ================= START TRACK =================

async def start_track(player: MusicPlayer, track, auto: bool):

    player.current_track = track
    player.track_start_time = time.time()

    try:
        await player.play(track)
    except Exception as e:
        logger.exception("Ошибка запуска трека: %s", e)
        return

    try:
        await player.set_volume(100)
    except Exception:
        logger.exception("Не удалось установить громкость плеера")

    if player.control_message:
        await safe_message_edit(
            player.control_message,
            embed=build_embed(player),
            view=MusicControls(player)
        )


# ================= CONTROL MESSAGE =================

async def send_control_message(interaction: discord.Interaction, player: MusicPlayer):

    embed = discord.Embed(
        title="🎵 Музыка",
        description="Загрузка...",
        color=0x57F287,
    )

    message = await interaction.followup.send(
        embed=embed,
        view=MusicControls(player)
    )

    player.control_message = message


async def send_temporary_followup(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view=None,
    delete_after: int = 5,
):

    message = await interaction.followup.send(
        content=content,
        embed=embed,
        view=view,
        wait=True,
        delete_after=delete_after,
    )

    async def _delete_later():
        try:
            await asyncio.sleep(delete_after)
            if not message.flags.ephemeral:
                await message.delete()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Failed to delete temporary followup message")

    asyncio.create_task(_delete_later())
    return message
