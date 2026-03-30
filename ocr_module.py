import asyncio
import logging
import os
import re
import tempfile

import discord
import wavelink

from PIL import Image, ImageFilter, ImageOps
from discord import app_commands
from discord.ext import commands

from music_core import MusicPlayer, start_track, send_control_message, send_temporary_followup

MAX_FILE_SIZE = 10 * 1024 * 1024
OCR_TIMEOUT = 90
SEARCH_TIMEOUT = 12
MAX_OCR_TRACKS = 8
OCR_MAX_SIDE = 1600

logger = logging.getLogger(__name__)

reader = None
reader_lock = asyncio.Lock()
reader_preload_task: asyncio.Task | None = None


def _build_reader():

    import easyocr

    return easyocr.Reader(["ru", "en"], gpu=False, verbose=False)


def _prepare_ocr_image(source_path: str) -> str:

    with Image.open(source_path) as image:
        prepared = image.convert("RGB")
        prepared.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.Resampling.LANCZOS)
        prepared = ImageOps.autocontrast(prepared)
        prepared = prepared.filter(ImageFilter.SHARPEN)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            prepared.save(tmp.name, format="PNG", optimize=True)
            return tmp.name


def _read_ocr_text(reader_obj, path: str):

    return reader_obj.readtext(
        path,
        detail=0,
        paragraph=False,
        decoder="greedy",
        beamWidth=1,
        batch_size=1,
        workers=0,
        canvas_size=OCR_MAX_SIDE,
        mag_ratio=1.0,
        text_threshold=0.7,
        low_text=0.4,
        link_threshold=0.4,
    )


async def get_reader():
    global reader

    async with reader_lock:
        if reader is None:
            try:
                reader = await asyncio.to_thread(_build_reader)
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Модуль easyocr не установлен. Установи зависимости из requirements.txt"
                ) from exc
            except Exception as exc:
                raise RuntimeError("OCR не удалось инициализировать") from exc

    return reader


async def run_ocr(path: str) -> list[str]:

    prepared_path = None

    try:
        prepared_path = await asyncio.to_thread(_prepare_ocr_image, path)
        reader_obj = await get_reader()
        result = await asyncio.to_thread(_read_ocr_text, reader_obj, prepared_path)
        return [str(line).strip() for line in result if str(line).strip()]
    finally:
        if prepared_path and os.path.exists(prepared_path):
            try:
                os.remove(prepared_path)
            except OSError:
                logger.warning("Не удалось удалить временный OCR-prep файл: %s", prepared_path)


async def preload_reader():

    try:
        await get_reader()
        logger.info("OCR reader preloaded")
    except Exception as exc:
        logger.warning("OCR preload failed: %s", exc)


def extract_tracks(lines):

    cleaned = []

    for line in lines:
        line = line.strip()
        line = re.sub(r'[|•…"“”]', '', line)
        line = re.sub(r';', '', line)
        line = re.sub(r'\s+', ' ', line)

        if len(line) < 2:
            continue

        cleaned.append(line)

    logger.info("========== OCR ==========")
    for line in cleaned:
        logger.info("OCR: %s", line)
    logger.info("========== END OCR ==========")

    tracks = []

    for i in range(0, len(cleaned) - 1, 2):
        title = cleaned[i]
        artist = cleaned[i + 1]
        tracks.append((artist.strip(), title.strip()))

    return tracks[:MAX_OCR_TRACKS]


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
                ephemeral=True,
            )
            return

        if not image.content_type or not image.content_type.startswith("image"):
            await interaction.response.send_message(
                "❌ Нужен файл изображения",
                ephemeral=True,
            )
            return

        if image.size > MAX_FILE_SIZE:
            await interaction.response.send_message(
                "❌ Файл слишком большой (макс 10MB)",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        path = None

        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                await image.save(tmp.name)
                path = tmp.name

            text_lines = await asyncio.wait_for(run_ocr(path), timeout=OCR_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "❌ OCR занял слишком много времени. Если бот только что перезапустили, подожди немного и попробуй ещё раз.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Ошибка OCR: {exc}",
                ephemeral=True,
            )
            return
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.warning("Не удалось удалить временный OCR файл: %s", exc)

        tracks = extract_tracks(text_lines)

        if not tracks:
            await interaction.followup.send(
                "❌ Треки не распознаны",
                ephemeral=True,
            )
            return

        await send_temporary_followup(
            interaction,
            content=f"🔎 Распознано треков: **{len(tracks)}**. Ищу совпадения...",
            delete_after=5,
        )

        player = interaction.guild.voice_client

        if not player:
            player = await interaction.user.voice.channel.connect(cls=MusicPlayer)

        added = []

        for artist, title in tracks:
            query = f"{artist} {title}"

            try:
                results = await asyncio.wait_for(wavelink.Playable.search(query), timeout=SEARCH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("OCR search timed out for query=%s", query)
                continue
            except Exception:
                logger.exception("OCR search failed for query=%s", query)
                continue

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
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎵 Добавлено в очередь",
            description="\n".join(f"{i+1}. {track}" for i, track in enumerate(added)),
            color=0xF1C40F,
        )

        await send_temporary_followup(interaction, embed=embed, delete_after=5)


async def setup(bot):
    global reader_preload_task

    await bot.add_cog(OCRMusic(bot))

    if reader_preload_task is None or reader_preload_task.done():
        reader_preload_task = asyncio.create_task(preload_reader())
