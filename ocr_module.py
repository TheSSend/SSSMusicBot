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
OCR_START_TIMEOUT = 180
OCR_REQUEST_TIMEOUT = 60
SEARCH_TIMEOUT = 12
MAX_OCR_TRACKS = 8
OCR_WORKER_PATH = os.path.join(os.path.dirname(__file__), "ocr_worker.py")

logger = logging.getLogger(__name__)

_ocr_proc: asyncio.subprocess.Process | None = None
_ocr_lock = asyncio.Lock()


async def ensure_ocr_worker() -> asyncio.subprocess.Process:

    global _ocr_proc

    async with _ocr_lock:
        if _ocr_proc is not None and _ocr_proc.returncode is None:
            return _ocr_proc

        _ocr_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            OCR_WORKER_PATH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assert _ocr_proc.stdout is not None
        ready_line = await asyncio.wait_for(_ocr_proc.stdout.readline(), timeout=OCR_START_TIMEOUT)
        if not ready_line:
            stderr_text = ""
            if _ocr_proc.stderr is not None:
                stderr_text = (await _ocr_proc.stderr.read()).decode("utf-8", errors="ignore").strip()
            raise RuntimeError(stderr_text or "OCR worker failed to start")

        payload = json.loads(ready_line.decode("utf-8"))
        if not payload.get("ready"):
            raise RuntimeError(payload.get("error") or "OCR worker is not ready")

        logger.info("OCR worker is ready")
        return _ocr_proc


async def run_ocr(path: str) -> list[str]:

    proc = await ensure_ocr_worker()
    assert proc.stdin is not None
    assert proc.stdout is not None

    request = json.dumps({"path": path}, ensure_ascii=False) + "\n"

    async with _ocr_lock:
        if proc.returncode is not None:
            raise RuntimeError("OCR worker is not running")

        proc.stdin.write(request.encode("utf-8"))
        await proc.stdin.drain()

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=OCR_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise

    if not line:
        raise RuntimeError("OCR worker returned empty response")

    try:
        payload = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("OCR worker returned invalid response") from exc

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "OCR worker failed")

    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise RuntimeError("OCR worker returned invalid lines")

    return [str(line).strip() for line in lines if str(line).strip()]


async def preload_ocr_worker():

    try:
        await ensure_ocr_worker()
    except Exception as exc:
        logger.warning("OCR worker preload failed: %s", exc)


def extract_tracks(lines):

    cleaned = []

    for l in lines:
        l = l.strip()
        l = re.sub(r'[|•…"“”]', '', l)
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

            text_lines = await run_ocr(path)

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
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎵 Добавлено в очередь",
            description="\n".join(f"{i+1}. {t}" for i, t in enumerate(added)),
            color=0xf1c40f,
        )

        await send_temporary_followup(interaction, embed=embed, delete_after=5)


async def setup(bot):
    await bot.add_cog(OCRMusic(bot))
    asyncio.create_task(preload_ocr_worker())
