import discord
import wavelink
import asyncio
import re
import tempfile
import os
import logging

from discord import app_commands
from discord.ext import commands

from music_core import MusicPlayer, start_track, send_control_message, send_temporary_followup

MAX_FILE_SIZE = 10 * 1024 * 1024

logger = logging.getLogger(__name__)

reader = None
reader_lock = asyncio.Lock()


# ================= SAFE EASY OCR =================

async def get_reader():
    global reader

    async with reader_lock:
        if reader is None:
            try:
                import easyocr
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Модуль easyocr не установлен. Установи зависимости из requirements.txt"
                ) from exc

            loop = asyncio.get_running_loop()
            reader = await loop.run_in_executor(
                None,
                lambda: easyocr.Reader(['ru', 'en'], gpu=False, verbose=False)
            )

    return reader


async def run_ocr(path):
    loop = asyncio.get_running_loop()
    r = await get_reader()

    result = await loop.run_in_executor(
        None,
        lambda: r.readtext(path, detail=0)
    )

    return result


# ================= TRACK PARSER =================

def extract_tracks(lines):

    cleaned = []

    for l in lines:
        l = l.strip()
        l = re.sub(r'[|•…"“”"]', '', l)
        l = re.sub(r';', '', l)
        l = re.sub(r'\s+', ' ', l)

        if len(l) < 2:
            continue

        cleaned.append(l)

    logger.info("========== OCR ==========")
    for line in cleaned:
        logger.info("OCR: %s", line)
    logger.info("========== END OCR ==========")

    tracks = []

    # просто берём по 2 строки
    for i in range(0, len(cleaned) - 1, 2):
        title = cleaned[i]
        artist = cleaned[i + 1]

        tracks.append((artist.strip(), title.strip()))

    return tracks[:20]


# ================= COG =================

class OCRMusic(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="playimage",
        description="Добавить треки по скриншоту"
    )
    async def playimage(self, interaction: discord.Interaction, image: discord.Attachment):

        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ Ты не в голосовом канале",
                ephemeral=True
            )
            return

        if not image.content_type or not image.content_type.startswith("image"):
            await interaction.response.send_message(
                "❌ Нужен файл изображения",
                ephemeral=True
            )
            return

        if image.size > MAX_FILE_SIZE:
            await interaction.response.send_message(
                "❌ Файл слишком большой (макс 10MB)",
                ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        path = None

        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                await image.save(tmp.name)
                path = tmp.name

            text_lines = await asyncio.wait_for(run_ocr(path), timeout=30)

        except asyncio.TimeoutError:
            await interaction.followup.send(
                "❌ OCR занял слишком много времени",
                ephemeral=True
            )
            return

        except Exception as e:
            await interaction.followup.send(
                f"❌ Ошибка OCR: {e}",
                ephemeral=True
            )
            return

        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    logger.warning("Не удалось удалить временный OCR файл: %s", e)

        tracks = extract_tracks(text_lines)

        if not tracks:
            await interaction.followup.send(
                "❌ Треки не распознаны",
                ephemeral=True
            )
            return

        player = interaction.guild.voice_client

        if not player:
            player = await interaction.user.voice.channel.connect(
                cls=MusicPlayer
            )

        added = []

        for artist, title in tracks:

            results = await wavelink.Playable.search(f"{artist} {title}")

            if not results:
                continue

            track = results[0]
            track.requester = interaction.user

            if not player.playing and not player.paused:
                await start_track(player, track, False)

                if not player.control_message:
                    await send_control_message(interaction, player)
            else:
                await player.queue.put_wait(track)

            added.append(f"{artist} – {title}")

        if not added:
            await interaction.followup.send(
                "❌ Ничего не найдено в поиске",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎵 Добавлено в очередь",
            description="\n".join(
                f"{i+1}. {t}" for i, t in enumerate(added)
            ),
            color=0xf1c40f
        )

        await send_temporary_followup(interaction, embed=embed, delete_after=5)


async def setup(bot):
    await bot.add_cog(OCRMusic(bot))
