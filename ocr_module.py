import asyncio
import difflib
import logging
import os
import re
import time
import tempfile

import discord
import wavelink

from PIL import Image, ImageFilter, ImageOps
from discord import app_commands
from discord.ext import commands

from music_core import MusicPlayer, start_track, send_control_message, send_temporary_followup, display_author

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

MAX_FILE_SIZE = 10 * 1024 * 1024
OCR_TIMEOUT = 90
OCR_INIT_TIMEOUT = 120
SEARCH_TIMEOUT = 12
MAX_OCR_TRACKS = 8
MAX_SEARCH_CANDIDATES = 10
OCR_MAX_SIDE = 1600
OCR_MIN_SCORE = 0.35
OCR_LINE_Y_THRESHOLD = 10

OCR_PHRASE_CORRECTIONS = {
    "mrpuctoe": "Игристое",
    "mrpnctoe": "Игристое",
    "mrpuctoehasman": "Игристое",
    "mproctи": "Прости",
    "mpoctи": "Прости",
    "pocth": "Прости",
    "pостн": "Прости",
    "te6eheomeh": "Тебе не до меня",
    "te6eheaomeha": "Тебе не до меня",
    "corpeimeha": "согрей меня",
    "corpenmeha": "согрей меня",
    "3bohnkoraa3axoyewbremix": "Звони, когда захочешь Remix",
    "onahawenio6bntusovkasoulfremix": "Ода нашей любви TUSOVKA & SOULF Remix",
    "oaahaweinio6bntusovkasoulfremix": "Ода нашей любви TUSOVKA & SOULF Remix",
    "mlonbitkahomep5": "Попытка номер 5",
    "mlonbitkanomep5": "Попытка номер 5",
    "monbithomep5": "Попытка номер 5",
    "monbithomep5marvinkvartira": "Попытка номер 5",
    "kapaxobmonamvgma": "Джарахов, MONA, MVGMA",
    "aanh": "АДЛИН",
    "ainh": "АДЛИН",
    "ahtohhe6okrause": "Антон Небо, Krause",
}

logger = logging.getLogger(__name__)

ocr_engine = None
ocr_engine_lock = asyncio.Lock()


def _build_ocr_engine():
    from paddleocr import PaddleOCR

    return PaddleOCR(
        device="cpu",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_hpi=False,
        enable_mkldnn=False,
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="eslav_PP-OCRv5_mobile_rec",
    )


def _prepare_ocr_image(source_path: str) -> str:

    with Image.open(source_path) as image:
        prepared = image.convert("RGB")
        prepared.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.Resampling.LANCZOS)
        prepared = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.LANCZOS)
        prepared = ImageOps.autocontrast(prepared)
        prepared = prepared.filter(ImageFilter.SHARPEN)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            prepared.save(tmp.name, format="PNG", optimize=True)
            return tmp.name


def _prepare_ocr_binary_image(source_path: str) -> str:

    with Image.open(source_path) as image:
        prepared = image.convert("L")
        prepared.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.Resampling.LANCZOS)
        prepared = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.LANCZOS)
        prepared = ImageOps.autocontrast(prepared)
        prepared = prepared.filter(ImageFilter.SHARPEN)
        prepared = prepared.point(lambda px: 255 if px > 170 else 0)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            prepared.save(tmp.name, format="PNG", optimize=True)
            return tmp.name


def _iter_paddle_results(result):
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return list(result)
    try:
        return list(result)
    except TypeError:
        return [result]


def _coerce_sequence(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        if hasattr(value, "tolist"):
            value = value.tolist()
            if isinstance(value, list):
                return value
    except Exception:
        pass
    try:
        return list(value)
    except TypeError:
        return [value]


def _unwrap_paddle_result(item):
    payload = getattr(item, "res", None)
    if payload is None:
        payload = getattr(item, "json", None)
    if isinstance(payload, dict):
        payload = payload.get("res", payload)
    if payload is None and isinstance(item, dict):
        payload = item.get("res", item.get("json", item))
    return payload or {}


def _extract_lines_from_paddle_result(item) -> list[str]:
    payload = _unwrap_paddle_result(item)
    if not isinstance(payload, dict):
        return []

    rec_texts = payload.get("rec_texts")
    if rec_texts is None:
        rec_text = payload.get("rec_text")
        rec_texts = _coerce_sequence(rec_text)
    else:
        rec_texts = _coerce_sequence(rec_texts)

    rec_scores = payload.get("rec_scores")
    if rec_scores is None:
        rec_score = payload.get("rec_score")
        rec_scores = _coerce_sequence(rec_score)
    else:
        rec_scores = _coerce_sequence(rec_scores)

    rec_boxes = payload.get("rec_boxes")
    if rec_boxes is None:
        rec_boxes = payload.get("rec_polys")
    if rec_boxes is None:
        rec_boxes = payload.get("dt_polys")
    if rec_boxes is None:
        rec_boxes = []
    else:
        rec_boxes = _coerce_sequence(rec_boxes)

    parts = []
    for index, text in enumerate(rec_texts):
        text = str(text).strip()
        if not text:
            continue

        try:
            score = float(rec_scores[index]) if index < len(rec_scores) else 1.0
        except (TypeError, ValueError):
            score = 1.0

        if score < OCR_MIN_SCORE:
            continue

        try:
            box = rec_boxes[index]
        except Exception:
            continue

        points = []
        for point in box:
            try:
                x, y = float(point[0]), float(point[1])
            except Exception:
                continue
            points.append((x, y))

        if not points:
            continue

        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
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


def _extract_lines_from_ocr_result(result) -> list[str]:

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


def line_quality_score(value: str) -> int:

    cyr = sum(1 for ch in value if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    lat = sum(1 for ch in value if "a" <= ch.lower() <= "z")
    digits = sum(1 for ch in value if ch.isdigit())
    spaces = value.count(" ")
    return (cyr * 3) + lat + digits - spaces


def correction_key(value: str) -> str:

    normalized = replace_confusable_latin_with_cyrillic(value)
    normalized = aggressive_ocr_title_guesses(normalized)[0] if aggressive_ocr_title_guesses(normalized) else normalized
    return re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9]", "", normalized).lower()


def correct_ocr_phrase(value: str) -> str:

    corrected = OCR_PHRASE_CORRECTIONS.get(correction_key(value))
    return corrected or value


def merge_ocr_lines(primary: list[str], secondary: list[str]) -> list[str]:

    if not secondary:
        return primary

    if len(primary) != len(secondary):
        return secondary if len(secondary) > len(primary) else primary

    merged = []
    for first, second in zip(primary, secondary):
        merged.append(second if line_quality_score(second) > line_quality_score(first) else first)

    return merged


def log_ocr_lines(stage: str, lines: list[str]) -> None:
    preview = " | ".join(lines[:20]) if lines else "<empty>"
    logger.info("OCR stage=%s count=%s preview=%s", stage, len(lines), preview)


def _run_ocr(engine, path: str, binary_path: str | None = None) -> list[str]:
    result = engine.predict(path)
    lines = []
    for item in _iter_paddle_results(result):
        lines.extend(_extract_lines_from_paddle_result(item))
    log_ocr_lines("primary", lines)

    if binary_path is None or len(lines) >= 3:
        return lines

    binary_result = engine.predict(binary_path)
    binary_lines = []
    for item in _iter_paddle_results(binary_result):
        binary_lines.extend(_extract_lines_from_paddle_result(item))
    log_ocr_lines("binary", binary_lines)
    merged = merge_ocr_lines(lines, binary_lines)
    log_ocr_lines("merged", merged)
    return merged


async def get_ocr_engine():
    global ocr_engine

    async with ocr_engine_lock:
        if ocr_engine is None:
            try:
                ocr_engine = await asyncio.to_thread(_build_ocr_engine)
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Модуль paddleocr не установлен. Установи зависимости из requirements.txt"
                ) from exc
            except Exception as exc:
                raise RuntimeError("OCR не удалось инициализировать") from exc

    return ocr_engine


async def run_ocr(path: str) -> list[str]:

    prepared_path = None
    binary_path = None

    try:
        started_at = time.monotonic()
        prepared_path = await asyncio.to_thread(_prepare_ocr_image, path)
        binary_path = await asyncio.to_thread(_prepare_ocr_binary_image, path)
        engine = await get_ocr_engine()
        lines = await asyncio.to_thread(_run_ocr, engine, prepared_path, binary_path)
        logger.info("OCR finished in %.2fs and returned %s lines", time.monotonic() - started_at, len(lines))
        return lines
    finally:
        for temp_path in (prepared_path, binary_path):
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logger.warning("Не удалось удалить временный OCR-prep файл: %s", temp_path)


def extract_tracks(lines):

    cleaned = []

    for line in lines:
        line = correct_ocr_phrase(line)
        line = line.strip()
        line = re.sub(r'[|•…"“”]', '', line)
        line = re.sub(r';', '', line)
        line = re.sub(r'(^|\s)(18\+|16\+|13\+|12\+|6\+|0\+)(?=\s|$)', ' ', line)
        line = re.sub(r'\s+', ' ', line)

        if len(line) < 2:
            continue

        cleaned.append(line)

    logger.info("========== OCR ==========")
    for line in cleaned:
        logger.info("OCR: %s", line)
    logger.info("========== END OCR ==========")

    tracks = []
    separator_patterns = (
        r"\s+-\s+",
        r"\s+—\s+",
        r"\s+–\s+",
        r"\s+\|\s+",
        r"\s+:\s+",
    )

    def add_track_candidate(title: str, artist: str = "") -> None:
        title = title.strip()
        artist = artist.strip()

        if not title:
            return

        candidate = (title, artist)
        if candidate not in tracks:
            tracks.append(candidate)

    for line in cleaned:
        split_candidate = None
        for pattern in separator_patterns:
            parts = re.split(pattern, line, maxsplit=1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                split_candidate = (parts[0].strip(), parts[1].strip())
                logger.info("OCR split candidate: title=%s artist=%s", split_candidate[0], split_candidate[1])
                break

        if split_candidate:
            add_track_candidate(*split_candidate)

    for i in range(0, len(cleaned) - 1, 2):
        title = cleaned[i]
        artist = cleaned[i + 1]
        logger.info("OCR paired candidate: title=%s artist=%s", title, artist)
        add_track_candidate(title, artist)

    if not tracks:
        logger.info("OCR tracks not built from pairs, falling back to single-line candidates")
        for line in cleaned:
            logger.info("OCR single-line candidate: %s", line)
            add_track_candidate(line, "")

    logger.info("OCR track candidates count=%s", len(tracks))
    return tracks[:MAX_OCR_TRACKS]


def normalize_ocr_text(value: str) -> str:

    value = value.replace("_", " ")
    value = value.replace(",", " ")
    value = value.replace("/", " ")
    value = value.replace("\\", " ")
    value = value.replace("Ё", "Е").replace("ё", "е")
    value = re.sub(r"(^|\s)(18\+|16\+|13\+|12\+|6\+|0\+)(?=\s|$)", " ", value)
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


def aggressive_ocr_title_guesses(value: str) -> list[str]:

    base = replace_confusable_latin_with_cyrillic(value)
    tables = [
        str.maketrans({
            "M": "И", "m": "и",
            "r": "г", "R": "Г",
            "p": "р", "P": "Р",
            "n": "и", "N": "И",
            "c": "с", "C": "С",
            "T": "т", "t": "т",
            "A": "д", "a": "а",
            "K": "к", "k": "к",
            "H": "н", "h": "н",
            "O": "о", "o": "о",
            "E": "е", "e": "е",
        }),
        str.maketrans({
            "M": "м", "m": "м",
            "A": "д", "a": "а",
            "K": "к", "k": "к",
            "H": "н", "h": "н",
            "p": "р", "P": "Р",
            "n": "п", "N": "П",
            "c": "с", "C": "С",
            "T": "т", "t": "т",
            "O": "о", "o": "о",
            "E": "е", "e": "е",
        }),
    ]

    variants = []
    for table in tables:
        candidate = base.translate(table)
        if candidate not in variants:
            variants.append(candidate)

    return variants


def build_match_text(value: str) -> str:

    value = normalize_ocr_text(value)
    value = replace_confusable_latin_with_cyrillic(value)
    value = value.lower()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def build_title_candidates(value: str) -> list[str]:

    candidates = []
    raw_candidates = [
        build_match_text(value),
        build_match_text(replace_confusable_latin_with_cyrillic(value)),
    ]

    raw_candidates.extend(build_match_text(candidate) for candidate in aggressive_ocr_title_guesses(value))

    for candidate in raw_candidates:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    return candidates


def compact_match_text(value: str) -> str:

    return build_match_text(value).replace(" ", "")


def similarity_score(left: str, right: str) -> float:

    if not left or not right:
        return 0.0

    return difflib.SequenceMatcher(None, left, right).ratio()


def has_alt_version_marker(value: str) -> bool:

    normalized = build_match_text(value)
    markers = (
        "remix",
        "sped up",
        "speed up",
        "slowed",
        "reverb",
        "nightcore",
        "mashup",
        "edit",
        "version",
        "cover",
        "live",
    )
    return any(marker in normalized for marker in markers)


def clean_title_extras(value: str) -> str:

    normalized = build_match_text(value)
    normalized = re.sub(r"\b(remix|hardstyle|sped up|speed up|slowed|reverb|nightcore|mashup|edit|version|cover|live|official audio|official video)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def score_track_match(track, title: str, artist: str) -> float:

    title_candidates = build_title_candidates(title)
    expected_title = title_candidates[0] if title_candidates else ""
    expected_artist = build_match_text(artist)
    actual_title = build_match_text(getattr(track, "title", "") or "")
    actual_artist = build_match_text(getattr(track, "author", "") or "")
    compact_expected_titles = [candidate.replace(" ", "") for candidate in title_candidates]
    compact_actual_title = compact_match_text(getattr(track, "title", "") or "")
    cleaned_actual_title = clean_title_extras(getattr(track, "title", "") or "")
    cleaned_actual_title_score = max((similarity_score(candidate, cleaned_actual_title) for candidate in title_candidates), default=0.0)

    title_score = max((similarity_score(candidate, actual_title) for candidate in title_candidates), default=0.0)
    artist_score = similarity_score(expected_artist, actual_artist)

    title_tokens = set(expected_title.split())
    artist_tokens = set(expected_artist.split())
    actual_title_tokens = set(actual_title.split())
    actual_artist_tokens = set(actual_artist.split())

    title_overlap = len(title_tokens & actual_title_tokens) / max(len(title_tokens), 1)
    artist_overlap = len(artist_tokens & actual_artist_tokens) / max(len(artist_tokens), 1)

    artist_bonus = 0.15 if expected_artist and expected_artist in actual_artist else 0.0
    title_bonus = 0.1 if expected_title and expected_title in actual_title else 0.0
    version_penalty = 0.0
    clean_title_bonus = 0.0

    if has_alt_version_marker(getattr(track, "title", "") or "") and not has_alt_version_marker(title):
        version_penalty += 0.45

    if has_alt_version_marker(getattr(track, "author", "") or "") and not has_alt_version_marker(artist):
        version_penalty += 0.12

    extra_word_penalty = max(0, len(actual_title.split()) - len(cleaned_actual_title.split())) * 0.05

    for compact_expected_title in compact_expected_titles:
        if compact_expected_title and compact_expected_title in compact_actual_title:
            clean_title_bonus = max(clean_title_bonus, 0.16)

        if compact_expected_title and compact_expected_title == compact_match_text(cleaned_actual_title):
            clean_title_bonus = max(clean_title_bonus, 0.36)

    return (
        (title_score * 0.3)
        + (cleaned_actual_title_score * 0.2)
        + (artist_score * 0.35)
        + (title_overlap * 0.1)
        + (artist_overlap * 0.15)
        + artist_bonus
        + title_bonus
        + clean_title_bonus
        - version_penalty
        - extra_word_penalty
    )


def build_ocr_search_queries(title: str, artist: str) -> list[str]:

    raw_title = title.strip()
    raw_artist = artist.strip()
    title_clean = normalize_ocr_text(raw_title)
    artist_clean = normalize_ocr_text(raw_artist)
    title_cyr = replace_confusable_latin_with_cyrillic(title_clean)
    title_guesses = aggressive_ocr_title_guesses(title_clean)
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

    for title_guess in title_guesses:
        variants.append(title_guess)
        if artist_cyr:
            variants.append(f"{title_guess} {artist_cyr}")

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
            logger.info("OCR returned %s raw lines for attachment=%s", len(text_lines), getattr(image, "filename", None))
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "❌ OCR занял слишком много времени",
                ephemeral=True,
            )
            return
        except Exception as exc:
            error_text = str(exc)
            if "ConvertPirAttribute2RuntimeAttribute" in error_text:
                await interaction.followup.send(
                    "❌ OCR сломан из-за несовместимых версий Paddle. Переустанови `paddlepaddle==3.2.0` и `paddleocr==3.3.3`, затем перезапусти бота.",
                    ephemeral=True,
                )
                return
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
        logger.info("OCR extracted %s track pairs", len(tracks))

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

            added.append(f"{display_author(getattr(track, 'author', None))} – {track.title}")

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
    try:
        await asyncio.wait_for(get_ocr_engine(), timeout=OCR_INIT_TIMEOUT)
        logger.info("OCR engine warmed up successfully")
    except Exception:
        logger.exception("OCR engine warmup failed")
