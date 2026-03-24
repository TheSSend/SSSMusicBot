import discord
import wavelink
import asyncio
import time
import logging

from wavelink import LavalinkException

from edit_guard import safe_message_edit

logger = logging.getLogger(__name__)

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
        return "⬜" * 20

    ratio = elapsed / total
    filled = int(ratio * 20)

    if filled > 20:
        filled = 20

    return "🟩" * filled + "⬜" * (20 - filled)


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
        description=(
            f"**{track.title}**\n"
            f"👤 {track.author}\n\n"
            f"`{elapsed//60:02}:{elapsed%60:02}` "
            f"{bar} "
            f"`{total//60:02}:{total%60:02}`"
        ),
        color=0x2ecc71
    )

    return embed


# ================= CONTROLS =================

class MusicControls(discord.ui.View):

    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, _):

        if not self.player:
            return

        await interaction.response.defer()

        await self.player.pause(not self.player.paused)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.primary)
    async def skip(self, interaction: discord.Interaction, _):

        await interaction.response.defer()

        if self.player:
            await self.player.stop()

    @discord.ui.button(label="📜", style=discord.ButtonStyle.success)
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

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _):

        await interaction.response.defer()

        if self.player:
            await self.player.disconnect()


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
        color=0x2ecc71
    )

    message = await interaction.followup.send(
        embed=embed,
        view=MusicControls(player)
    )

    player.control_message = message
