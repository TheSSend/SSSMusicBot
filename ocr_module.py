import asyncio
import difflib
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
MAX_SEARCH_CANDIDATES = 10
OCR_MAX_SIDE = 1600
OCR_MIN_SCORE = 0.35
OCR_LINE_Y_THRESHOLD = 10

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
                "h": max(ys) - min(ys),
            }
        )

    parts.sort(key=lambda part: (part["y"], part["x"]))

    if not parts:
        return []

    lines = []
    current_line = [parts[0]]

    for part in parts[1:]:
        previous = current_line[-1]
        dynamic_threshold = max(
            OCR_LINE_Y_THRESHOLD,
            min(previous["h"], part["h"]) * 0.4,
        )

        if abs(part["y"] - previous["y"]) <= dynamic_threshold:
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
        line = re.sub(r'\b(18\+|16\+|13\+|12\+|6\+|0\+)\b', '', line)
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
        tracks.append((title.strip(), artist.strip()))

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


def replace_confusable_latin_with_cyrillic(value: str) -> str:

    translation = str.maketrans({
        "A": "А", "a": "а",
        "B": "В", "E": "Е", "e": "е",
        "H": "Н", "K": "К", "M": "М",
        "O": "О", "o": "о", "P": "Р",
        "C": "С", "c": "с", "T": "Т",
        "X": "Х", "x": "х", "Y": "У", "y": "у",
    })
    return value.translate(translation)


def build_match_text(value: str) -> str:

    value = normalize_ocr_text(value)
    value = replace_confusable_latin_with_cyrillic(value)
    value = value.lower()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def similarity_score(left: str, right: str) -> float:

    if not left or not right:
        return 0.0

    return difflib.SequenceMatcher(None, left, right).ratio()


def score_track_match(track, title: str, artist: str) -> float:

    expected_title = build_match_text(title)
    expected_artist = build_match_text(artist)
    actual_title = build_match_text(getattr(track, "title", "") or "")
    actual_artist = build_match_text(getattr(track, "author", "") or "")

    title_score = similarity_score(expected_title, actual_title)
    artist_score = similarity_score(expected_artist, actual_artist)

    title_tokens = set(expected_title.split())
    artist_tokens = set(expected_artist.split())
    actual_title_tokens = set(actual_title.split())
    actual_artist_tokens = set(actual_artist.split())

    title_overlap = len(title_tokens & actual_title_tokens) / max(len(title_tokens), 1)
    artist_overlap = len(artist_tokens & actual_artist_tokens) / max(len(artist_tokens), 1)

    artist_bonus = 0.15 if expected_artist and expected_artist in actual_artist else 0.0
    title_bonus = 0.1 if expected_title and expected_title in actual_title else 0.0

    return (
        (title_score * 0.3)
        + (artist_score * 0.35)
        + (title_overlap * 0.1)
        + (artist_overlap * 0.15)
        + artist_bonus
        + title_bonus
    )


def build_ocr_search_queries(title: str, artist: str) -> list[str]:

    raw_title = title.strip()
    raw_artist = artist.strip()
    title_clean = normalize_ocr_text(raw_title)
    artist_clean = normalize_ocr_text(raw_artist)
    title_cyr = replace_confusable_latin_with_cyrillic(title_clean)
    artist_cyr = replace_confusable_latin_with_cyrillic(artist_clean)

    variants = [
        f"{title_clean} {artist_clean}",
        title_clean,
        artist_clean,
        f"{artist_clean} {title_clean}",
        f"{title_cyr} {artist_cyr}",
        title_cyr,
        artist_cyr,
        f"{artist_cyr} {title_cyr}",
        f"{raw_title} {raw_artist}",
        raw_title,
        raw_artist,
        f"{raw_artist} {raw_title}",
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


async def search_ocr_track(title: str, artist: str):

    node = wavelink.Pool.get_node()
    best_track = None
    best_score = 0.0

    for query in build_ocr_search_queries(title, artist):
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

        if not results:
            continue

        logger.info(
            "OCR search matched query=%s type=%s len=%s",
            candidate,
            type(results).__name__,
            len(results) if hasattr(results, "__len__") else "?",
        )

        for track in list(results)[:10]:
            score = score_track_match(track, title, artist)
            logger.info(
                "OCR candidate score=%.3f query=%s track=%s author=%s",
                score,
                candidate,
                getattr(track, "title", None),
                getattr(track, "author", None),
            )
            if score > best_score:
                best_score = score
                best_track = track

        if best_score >= 0.68:
            break

    return best_track


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

        for title, artist in tracks:
            track = await search_ocr_track(title, artist)

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
