import asyncio
import json
import logging
import os
import re
import sys
import tempfile

import discord
import wavelink

from discord import app_commands
from discord.ext import commands

from music_core import MusicPlayer, start_track, send_control_message, send_temporary_followup

MAX_FILE_SIZE = 10 * 1024 * 1024
OCR_TIMEOUT = 180
SEARCH_TIMEOUT = 12
MAX_OCR_TRACKS = 8
MAX_SEARCH_CANDIDATES = 6
OCR_WORKER_PATH = os.path.join(os.path.dirname(__file__), "ocr_worker.py")

logger = logging.getLogger(__name__)


async def run_ocr(path: str) -> list[str]:

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        OCR_WORKER_PATH,
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=OCR_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    if proc.returncode != 0:
        error_text = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(error_text or f"OCR worker exited with code {proc.returncode}")

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("OCR worker returned invalid response") from exc

    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise RuntimeError("OCR worker returned invalid lines")

    return [str(line).strip() for line in lines if str(line).strip()]


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
        await send_temporary_followup(
            interaction,
            content="🖼️ Распознаю скриншот. Первый запуск после рестарта может занять до 2-3 минут.",
            delete_after=8,
        )

        path = None

        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                await image.save(tmp.name)
                path = tmp.name

            text_lines = await run_ocr(path)
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
