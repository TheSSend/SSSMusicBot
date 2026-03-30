import discord
import wavelink
import asyncio
import re
import tempfile
import os
import logging
import shutil

from PIL import Image, ImageOps, ImageFilter

from discord import app_commands
from discord.ext import commands

from music_core import MusicPlayer, start_track, send_control_message, send_temporary_followup

MAX_FILE_SIZE = 10 * 1024 * 1024
OCR_MAX_SIDE = 1600
OCR_ENGINE = os.getenv("OCR_ENGINE", "auto").strip().lower()

logger = logging.getLogger(__name__)

reader = None
reader_lock = asyncio.Lock()


# ================= SAFE EASY OCR =================


def _build_reader():

    import easyocr

    return easyocr.Reader(['ru', 'en'], gpu=False, verbose=False)


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


async def _run_tesseract_ocr(path: str) -> list[str]:

    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise FileNotFoundError("tesseract not found")

    proc = await asyncio.create_subprocess_exec(
        tesseract,
        path,
        "stdout",
        "--oem",
        "1",
        "--psm",
        "6",
        "-l",
        "rus+eng",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_text = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(error_text or f"tesseract exited with code {proc.returncode}")

    text = stdout.decode("utf-8", errors="ignore")
    return [line.strip() for line in text.splitlines() if line.strip()]

async def get_reader():
    global reader

    async with reader_lock:
        if reader is None:
            try:
                loop = asyncio.get_running_loop()
                reader = await loop.run_in_executor(None, _build_reader)
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Модуль easyocr не установлен. Установи зависимости из requirements.txt"
                ) from exc
            except Exception as exc:
                raise RuntimeError("OCR не удалось инициализировать") from exc

    return reader


async def run_ocr(path):
    loop = asyncio.get_running_loop()

    prepared_path = None

    try:
        prepared_path = await loop.run_in_executor(None, _prepare_ocr_image, path)

        if OCR_ENGINE in {"auto", "tesseract"}:
            try:
                return await _run_tesseract_ocr(prepared_path)
            except FileNotFoundError:
                if OCR_ENGINE == "tesseract":
                    raise RuntimeError("tesseract не установлен")
            except Exception as exc:
                if OCR_ENGINE == "tesseract":
                    raise RuntimeError(f"Tesseract OCR failed: {exc}") from exc
                logger.warning("Tesseract OCR failed, falling back to EasyOCR: %s", exc)

        r = await get_reader()
        result = await loop.run_in_executor(
            None,
            _read_ocr_text,
            r,
            prepared_path,
        )
    finally:
        if prepared_path and os.path.exists(prepared_path):
            try:
                os.remove(prepared_path)
            except OSError:
                logger.warning("Не удалось удалить временный OCR-prep файл: %s", prepared_path)

    return result


async def preload_ocr():

    if OCR_ENGINE == "tesseract" or (OCR_ENGINE == "auto" and shutil.which("tesseract")):
        logger.info("Tesseract OCR selected, EasyOCR preload skipped")
        return

    try:
        await get_reader()
        logger.info("OCR reader preloaded")
    except Exception as exc:
        logger.warning("OCR preload failed: %s", exc)


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
    asyncio.create_task(preload_ocr())
