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
OCR_TIMEOUT = 45
SEARCH_TIMEOUT = 12
MAX_OCR_TRACKS = 8
MAX_SEARCH_CANDIDATES = 6
OCR_MAX_SIDE = 1600
OCR_MIN_SCORE = 0.35
OCR_LINE_Y_THRESHOLD = 22

logger = logging.getLogger(__name__)

ocr_engine = None
ocr_engine_lock = asyncio.Lock()


def _build_ocr_engine():

    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _prepare_ocr_image(source_path: str) -> str:

    with Image.open(source_path) as image:
        prepared = image.convert("RGB")
        prepared.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.Resampling.LANCZOS)
        prepared = ImageOps.autocontrast(prepared)
        prepared = prepared.filter(ImageFilter.SHARPEN)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            prepared.save(tmp.name, format="PNG", optimize=True)
            return tmp.name


def _run_ocr(engine, path: str) -> list[str]:

    result, _elapsed = engine(path)
    if not result:
        return []

    parts = []
    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue

        box = item[0]
        text = str(item[1]).strip()
        try:
            score = float(item[2])
        except (TypeError, ValueError):
            score = 1.0

        if not text or score < OCR_MIN_SCORE:
            continue

        if not isinstance(box, (list, tuple)) or not box:
            continue

        points = [point for point in box if isinstance(point, (list, tuple)) and len(point) >= 2]
        if not points:
            continue

        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        parts.append(
            {
                "text": text,
                "x": min(xs),
                "y": sum(ys) / len(ys),
            }
        )

    parts.sort(key=lambda part: (part["y"], part["x"]))

    if not parts:
        return []

    lines = []
    current_line = [parts[0]]

    for part in parts[1:]:
        previous_y = current_line[-1]["y"]
        if abs(part["y"] - previous_y) <= OCR_LINE_Y_THRESHOLD:
            current_line.append(part)
            continue

        lines.append(" ".join(segment["text"] for segment in sorted(current_line, key=lambda item: item["x"])).strip())
        current_line = [part]

    if current_line:
        lines.append(" ".join(segment["text"] for segment in sorted(current_line, key=lambda item: item["x"])).strip())

    return lines


async def get_ocr_engine():
    global ocr_engine

    async with ocr_engine_lock:
        if ocr_engine is None:
            try:
                ocr_engine = await asyncio.to_thread(_build_ocr_engine)
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Модуль rapidocr-onnxruntime не установлен. Установи зависимости из requirements.txt"
                ) from exc
            except Exception as exc:
                raise RuntimeError("OCR не удалось инициализировать") from exc

    return ocr_engine


async def run_ocr(path: str) -> list[str]:

    prepared_path = None

    try:
        prepared_path = await asyncio.to_thread(_prepare_ocr_image, path)
        engine = await get_ocr_engine()
        return await asyncio.to_thread(_run_ocr, engine, prepared_path)
    finally:
        if prepared_path and os.path.exists(prepared_path):
            try:
                os.remove(prepared_path)
            except OSError:
                logger.warning("Не удалось удалить временный OCR-prep файл: %s", prepared_path)


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


def normalize_ocr_text(value: str) -> str:

    value = value.replace("_", " ")
    value = value.replace(",", " ")
    value = value.replace("/", " ")
    value = value.replace("\\", " ")
    value = value.replace("Ё", "Е").replace("ё", "е")
    value = re.sub(r"\b(18\+|16\+|12\+|6\+|0\+)\b", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def build_ocr_search_queries(artist: str, title: str) -> list[str]:

    raw_title = title.strip()
    raw_artist = artist.strip()
    title_clean = normalize_ocr_text(raw_title)
    artist_clean = normalize_ocr_text(raw_artist)

    variants = [
        f"{raw_artist} {raw_title}",
        f"{raw_title} {raw_artist}",
        raw_title,
        f"{artist_clean} {title_clean}",
        f"{title_clean} {artist_clean}",
        title_clean,
    ]

    queries = []
    seen = set()

    for variant in variants:
        normalized = re.sub(r"\s+", " ", variant).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        queries.append(normalized)

    return queries[:MAX_SEARCH_CANDIDATES]


async def search_ocr_track(artist: str, title: str):

    node = wavelink.Pool.get_node()

    for query in build_ocr_search_queries(artist, title):
        candidate = f"ytsearch:{query}"
        logger.info("OCR searching Lavalink with %s", candidate)

        try:
            results = await asyncio.wait_for(
                wavelink.Pool.fetch_tracks(candidate, node=node),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("OCR search timed out for query=%s", candidate)
            continue
        except wavelink.LavalinkException:
            logger.exception("OCR search failed for query=%s", candidate)
            continue
        except Exception:
            logger.exception("Unexpected OCR search failure for query=%s", candidate)
            continue

        if results:
            logger.info(
                "OCR search matched query=%s type=%s len=%s",
                candidate,
                type(results).__name__,
                len(results) if hasattr(results, "__len__") else "?",
            )
            return results[0]

    return None


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
                "❌ OCR занял слишком много времени",
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
            track = await search_ocr_track(artist, title)

            if not track:
                continue

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
    await bot.add_cog(OCRMusic(bot))
