import discord
import wavelink
import asyncio
import os
import time
import logging
import re
import aiohttp

from wavelink import LavalinkException

from edit_guard import safe_message_edit

logger = logging.getLogger(__name__)
logging.getLogger("lyricsgenius").setLevel(logging.WARNING)

def build_queue_preview(player: "MusicPlayer", limit: int = 10) -> str:
    items = list(player.queue)[:limit]
    if not items:
        return "Очередь пуста."

    lines = [f"{index + 1}. {track.title}" for index, track in enumerate(items)]
    remaining = len(player.queue) - len(items)
    if remaining > 0:
        lines.append(f"...и еще {remaining} треков")
    return "\n".join(lines)

def _user_in_same_voice_channel(interaction: discord.Interaction, player: "MusicPlayer") -> bool:
    user = getattr(interaction, "user", None)
    voice = getattr(user, "voice", None)
    if not voice or not getattr(voice, "channel", None):
        return False
    return getattr(voice.channel, "id", None) == getattr(getattr(player, "channel", None), "id", None)


async def _require_same_voice_channel(interaction: discord.Interaction, player: "MusicPlayer") -> bool:
    if _user_in_same_voice_channel(interaction, player):
        return True
    try:
        await interaction.followup.send("❌ Ты должен быть в том же голосовом канале, что и бот.", ephemeral=True)
    except Exception:
        pass
    return False

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

def normalize_lyrics_query(value: str | None) -> str:
    if not value:
        return ""

    normalized = " ".join(str(value).split()).strip()
    if not normalized:
        return ""

    noise_patterns = (
        r"\s*-\s*Topic$",
        r"\s*-\s*Official Video$",
        r"\s*-\s*Official Music Video$",
        r"\s*-\s*Official Audio$",
        r"\s*-\s*Lyrics$",
        r"\s*-\s*Lyric Video$",
        r"\s*\(Official Video\)$",
        r"\s*\(Official Music Video\)$",
        r"\s*\(Official Audio\)$",
        r"\s*\(Lyrics\)$",
        r"\s*\(Lyric Video\)$",
        r"\s*\(Topic\)$",
    )

    changed = True
    while changed:
        changed = False
        before = normalized
        for pattern in noise_patterns:
            normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE).strip()
        normalized = re.sub(r"\s+(?:\||/|-)\s*(?:topic|official audio|official video|official music video|lyrics|lyric video)$", "", normalized, flags=re.IGNORECASE).strip()
        normalized = re.sub(r"^provided to youtube by\s+", "", normalized, flags=re.IGNORECASE).strip()
        normalized = re.sub(r"^topic\s+", "", normalized, flags=re.IGNORECASE).strip()
        changed = normalized != before

    return normalized


def build_lyrics_search_variants(track_title: str, track_author: str) -> list[tuple[str, str]]:
    title = normalize_lyrics_query(track_title)
    author = normalize_lyrics_query(track_author)

    variants: list[tuple[str, str]] = []

    def add_variant(title_value: str, author_value: str) -> None:
        normalized_title = normalize_lyrics_query(title_value)
        normalized_author = normalize_lyrics_query(author_value)
        if not normalized_title:
            return
        candidate = (normalized_title, normalized_author)
        if candidate not in variants:
            variants.append(candidate)

    add_variant(title, author)

    if " - " in title and not author:
        left, right = [part.strip() for part in title.split(" - ", 1)]
        if left and right:
            add_variant(right, left)
            add_variant(left, right)

    if title.lower().endswith(" topic"):
        add_variant(title[:-6].strip(), author)

    if author.lower().endswith(" topic"):
        add_variant(title, author[:-6].strip())

    if not author:
        add_variant(title, "")

    return variants

LYRICS_CACHE_TTL_SECONDS = int(os.getenv("LYRICS_CACHE_TTL_SECONDS", "21600"))
_lyrics_cache: dict[str, tuple[float, str, str]] = {}

def _lyrics_cache_key(track) -> str:
    return "|".join(
        str(part or "").strip().lower()
        for part in (
            getattr(track, "encoded", None),
            getattr(track, "title", None),
            getattr(track, "author", None),
            getattr(track, "length", None),
        )
    )

def _lyrics_cache_get(track) -> tuple[str, str] | None:
    key = _lyrics_cache_key(track)
    cached = _lyrics_cache.get(key)
    if not cached:
        return None
    expires_at, source, lyrics_text = cached
    if time.time() >= expires_at:
        _lyrics_cache.pop(key, None)
        return None
    return source, lyrics_text

def _lyrics_cache_set(track, source: str, lyrics_text: str) -> None:
    key = _lyrics_cache_key(track)
    _lyrics_cache[key] = (time.time() + LYRICS_CACHE_TTL_SECONDS, source, lyrics_text)

# ================= PLAYER =================

class MusicPlayer(wavelink.Player):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = wavelink.Queue()
        self.control_message: discord.Message | None = None
        self.control_view: discord.ui.View | None = None
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
            logger.error("Lavalink rejected voice update guild=%s", getattr(self.guild, "id", None))
            await self.disconnect()
        except Exception:
            logger.exception("Unexpected error while dispatching voice update")
            raise
        else:
            self._connection_event.set()

# ================= STATE DUMPER =================

def dump_player_state(player: MusicPlayer, store):
    """Saves player state for Auto-Resume"""
    try:
        if not player.current_track:
            return
            
        guild_id_str = str(player.guild.id)
        state = store.load()

        queue_data = [getattr(t, "raw_data", None) or {"encoded": t.encoded} for t in player.queue]

        # Handle position calculation
        pos = getattr(player, "position", 0)  # sometimes provided by wavelink player object
        if pos == 0 and player.track_start_time:
            pos = int((time.time() - player.track_start_time) * 1000)

        pd = {
            "guild_id": player.guild.id,
            "channel_id": getattr(player.channel, "id", 0),
            "text_channel_id": getattr(player.control_message.channel, "id", 0) if player.control_message else 0,
            "control_message_id": getattr(player.control_message, "id", 0) if player.control_message else 0,
            "track_data": getattr(player.current_track, "raw_data", None) or {"encoded": player.current_track.encoded},
            "track_encoded": player.current_track.encoded,
            "position": pos,
            "queue_data": queue_data,
            "queue_encoded": [t.encoded for t in player.queue],
            "filters": {} # Future proofing for saving filters
        }
        
        state[guild_id_str] = pd
        store.save(state)
        
    except Exception:
        pass # ignore frequent dump errors

# ================= PROGRESS BAR =================

def progress_bar(elapsed, total):
    if total <= 0:
        return "░" * 10
    ratio = elapsed / total
    filled = int(ratio * 10)
    if filled > 10:
        filled = 10
    return "▰" * filled + "▱" * (10 - filled)

def embed_color(player: MusicPlayer) -> int:
    if getattr(player, "paused", False):
        return 0xF1C40F
    return 0x57F287

# ================= EMBED =================

def build_embed(player: MusicPlayer):
    track = player.current_track

    if not track:
        return discord.Embed(
            title="🎵 Музыка",
            description="Ничего не играет",
            color=0x2ecc71
        )

    if player.track_start_time is None:
        elapsed = getattr(player, "position", 0) // 1000
    else:
        elapsed = int(time.time() - player.track_start_time)
        
    total = int(track.length / 1000)

    if elapsed > total:
        elapsed = total

    bar = progress_bar(elapsed, total)

    embed = discord.Embed(
        title="🎵 Сейчас играет",
        color=embed_color(player),
    )

    embed.add_field(name="Трек", value=f"**{track.title}**", inline=False)
    embed.add_field(name="Исполнитель", value=display_author(track.author), inline=True)
    
    requester = getattr(track, "requester", None)
    embed.add_field(name="Включил", value=display_requester(requester), inline=True)

    embed.add_field(
        name="Прогресс",
        value=f"`{elapsed//60:02}:{elapsed%60:02}` {bar} `{total//60:02}:{total%60:02}`",
        inline=False,
    )

    artwork = getattr(track, "artwork", None)
    if artwork:
        embed.set_thumbnail(url=artwork)

    return embed


# ================= FILTERS MENU =================

class FiltersSelect(discord.ui.Select):
    def __init__(self, player: MusicPlayer | None = None):
        self.player = player
        options = [
            discord.SelectOption(label="Обычный (Без Эффектов)", value="reset", emoji="🔄", description="Отключить все эффекты"),
            discord.SelectOption(label="Bassboost", value="bassboost", emoji="🔊", description="Усиление низких частот"),
            discord.SelectOption(label="Nightcore", value="nightcore", emoji="🌙", description="Ускорение и высокий питч"),
            discord.SelectOption(label="Vaporwave", value="vaporwave", emoji="🌊", description="Замедление и низкий питч"),
        ]
        super().__init__(placeholder="🎛️ Фильтры звука...", min_values=1, max_values=1, options=options, custom_id="filter_select")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player = self.player
        if player is None:
            voice_client = getattr(getattr(interaction, "guild", None), "voice_client", None)
            if isinstance(voice_client, MusicPlayer):
                player = voice_client
        if not player:
            return
        if not await _require_same_voice_channel(interaction, player):
            return

        choice = self.values[0]
        filters = wavelink.Filters()

        if choice == "bassboost":
            # EQ implementation for basic bassboost
            filters.equalizer.set(bands=[
                {"band": 0, "gain": 0.25},
                {"band": 1, "gain": 0.25},
                {"band": 2, "gain": 0.15},
                {"band": 3, "gain": 0.05},
            ])
        elif choice == "nightcore":
            filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
        elif choice == "vaporwave":
            filters.timescale.set(speed=0.8, pitch=0.8, rate=1.0)
        
        try:
            await player.set_filters(filters)
            # Re-update the select text visually? Only if we wanted dynamic updating
            if player.control_message:
                 await safe_message_edit(
                    player.control_message,
                    embed=build_embed(player),
                    view=get_music_controls(player),
                )
        except Exception:
             logger.exception("Failed to apply Audio Filters")


# ================= CONTROLS =================

class MusicControls(discord.ui.View):
    def __init__(self, player: MusicPlayer | None = None):
        super().__init__(timeout=None)
        self.player = player
        
        # Add the Select Menu directly to the view
        self.add_item(FiltersSelect(player))


    @discord.ui.button(label="Пауза", emoji="⏸️", style=discord.ButtonStyle.secondary, row=1, custom_id="music_pause")
    async def pause(self, interaction: discord.Interaction, _):
        player = self.player or getattr(getattr(interaction, "guild", None), "voice_client", None)
        if not isinstance(player, MusicPlayer):
            return
        await interaction.response.defer()
        if not await _require_same_voice_channel(interaction, player):
            return

        try:
            await player.pause(not player.paused)
        except LavalinkException:
            try:
                await player.disconnect()
            except Exception:
                logger.exception("Failed to disconnect player after pause error")
        else:
            if player.control_message:
                try:
                    await safe_message_edit(
                        player.control_message,
                        embed=build_embed(player),
                        view=get_music_controls(player),
                    )
                except Exception:
                    logger.exception("Failed to refresh control message after pause")

    @discord.ui.button(label="Следующий", emoji="⏭️", style=discord.ButtonStyle.primary, row=1, custom_id="music_skip")
    async def skip(self, interaction: discord.Interaction, _):
        player = self.player or getattr(getattr(interaction, "guild", None), "voice_client", None)
        if not isinstance(player, MusicPlayer):
            return
        await interaction.response.defer()
        if player:
            if not await _require_same_voice_channel(interaction, player):
                return
            if player.queue.is_empty:
                await interaction.followup.send("📭 Очередь пуста", ephemeral=True)
                return
            try:
                await player.stop()
            except Exception:
                logger.exception("Failed to skip track")

    @discord.ui.button(label="Очередь", emoji="📜", style=discord.ButtonStyle.success, row=1, custom_id="music_queue")
    async def queue(self, interaction: discord.Interaction, _):
        player = self.player or getattr(getattr(interaction, "guild", None), "voice_client", None)
        if not isinstance(player, MusicPlayer):
            return
        await interaction.response.defer(ephemeral=True)
        if not await _require_same_voice_channel(interaction, player):
            return
        if player.queue.is_empty:
            await interaction.followup.send("📭 Очередь пуста", ephemeral=True)
            return
        text = "\n".join(
            f"{i+1}. {t.title}"
            for i, t in enumerate(list(player.queue)[:10])
        )
        if len(player.queue) > 10:
            text += f"\n...и еще {len(player.queue) - 10} треков"
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Текст", emoji="🎤", style=discord.ButtonStyle.secondary, row=2, custom_id="music_lyrics")
    async def lyrics(self, interaction: discord.Interaction, _):
        """Fetch lyrics from public API"""
        player = self.player or getattr(getattr(interaction, "guild", None), "voice_client", None)
        if not isinstance(player, MusicPlayer):
            return
        await interaction.response.defer(ephemeral=True)
        
        if not player.current_track:
            await interaction.followup.send("❌ Нет играющего трека.", ephemeral=True)
            return

        if not await _require_same_voice_channel(interaction, player):
            return

        track_title = player.current_track.title
        author = player.current_track.author

        clean_title = normalize_lyrics_query(track_title)
        clean_author = normalize_lyrics_query(author)
        lyric_variants = build_lyrics_search_variants(track_title, author)

        cached_lyrics = _lyrics_cache_get(player.current_track)
        if cached_lyrics is not None:
            cached_source, cached_text = cached_lyrics
            embed = discord.Embed(
                title=f"🎤 Текст: {track_title}",
                description=cached_text,
                color=0x9B59B6,
            )
            embed.set_footer(text=f"Источник: {cached_source} (cache)")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        genius_token = os.getenv("GENIUS_ACCESS_TOKEN", "").strip()
        if genius_token:
            try:
                import lyricsgenius

                genius = lyricsgenius.Genius(
                    genius_token,
                    timeout=10,
                    verbose=False,
                    remove_section_headers=True,
                skip_non_songs=True,
                excluded_terms=["(Remix)", "(Live)", "(Acoustic)"],
            )

                for title_value, artist_value in lyric_variants:
                    if not title_value:
                        continue

                    song = genius.search_song(title_value, artist_value or None)
                    lyrics_text = getattr(song, "lyrics", None) if song else None
                    if lyrics_text:
                        lyrics_text = str(lyrics_text).strip()
                        if len(lyrics_text) > 4000:
                            lyrics_text = lyrics_text[:3990] + "...\n(текст слишком длинный)"

                        embed = discord.Embed(
                            title=f"🎤 Текст: {track_title}",
                            description=lyrics_text,
                            color=0x9B59B6,
                        )
                        embed.set_footer(text="Источник: Genius")
                        _lyrics_cache_set(player.current_track, "Genius", lyrics_text)
                        _lyrics_cache_set(player.current_track, "some-random-api", lyrics_text)
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        return
            except ModuleNotFoundError:
                pass
            except Exception:
                logger.exception("Genius lyrics lookup failed")

        headers = {
            "User-Agent": "Musicbot/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for title_value, artist_value in lyric_variants:
                    if not title_value or not artist_value:
                        continue

                    params = {
                        "track_name": title_value,
                        "artist_name": artist_value,
                    }
                    if getattr(player.current_track, "album", None):
                        params["album_name"] = getattr(player.current_track, "album")
                    if getattr(player.current_track, "length", None):
                        params["duration"] = int(getattr(player.current_track, "length", 0) or 0) // 1000

                    for endpoint in ("https://lrclib.net/api/get", "https://lrclib.net/api/get_cached"):
                        try:
                            async with session.get(endpoint, params=params) as resp:
                                if resp.status == 200:
                                    payload = await resp.json()
                                    lyrics_text = (payload or {}).get("syncedLyrics") or (payload or {}).get("plainLyrics")
                                    if lyrics_text:
                                        lyrics_text = str(lyrics_text).strip()
                                        if len(lyrics_text) > 4000:
                                            lyrics_text = lyrics_text[:3990] + "...\n(текст слишком длинный)"
                                        embed = discord.Embed(
                                            title=f"🎤 Текст: {track_title}",
                                            description=lyrics_text,
                                            color=0x9B59B6,
                                        )
                                        embed.set_footer(text="Источник: LRCLIB")
                                        _lyrics_cache_set(player.current_track, "LRCLIB", lyrics_text)
                                        await interaction.followup.send(embed=embed, ephemeral=True)
                                        return
                        except Exception:
                            continue

                    try:
                        async with session.get(f"https://api.lyrics.ovh/v1/{artist_value}/{title_value}") as resp:
                            if resp.status == 200:
                                payload = await resp.json()
                                lyrics_text = str((payload or {}).get("lyrics") or "").strip()
                                if lyrics_text:
                                    if len(lyrics_text) > 4000:
                                        lyrics_text = lyrics_text[:3990] + "...\n(текст слишком длинный)"
                                    embed = discord.Embed(
                                        title=f"🎤 Текст: {track_title}",
                                        description=lyrics_text,
                                        color=0x9B59B6,
                                    )
                                    embed.set_footer(text="Источник: lyrics.ovh")
                                    _lyrics_cache_set(player.current_track, "lyrics.ovh", lyrics_text)
                                    await interaction.followup.send(embed=embed, ephemeral=True)
                                    return
                    except Exception:
                        pass
        except Exception:
            logger.exception("Failed to fetch lyrics from fallback services")

        normalized = clean_title or track_title
        # Using some-random-api as a stable fallback for lyrics
        api_url = "https://some-random-api.com/lyrics"
        
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(api_url, params={"title": f"{normalized} {author}".strip()}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        lyrics_text = data.get("lyrics", "")
                        
                        if len(lyrics_text) > 4000:
                            lyrics_text = lyrics_text[:3990] + "...\n(текст слишком длинный)"
                            
                        embed = discord.Embed(title=f"🎤 Текст: {track_title}", description=lyrics_text, color=0x9B59B6)
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        return
                    else:
                        await interaction.followup.send("❌ Текст не найден в открытой базе.", ephemeral=True)
        except Exception:
            logger.exception("Ошибка при запросе текста песни")
            await interaction.followup.send("❌ Возникла ошибка при поиске текста.", ephemeral=True)


    @discord.ui.button(label="Стоп", emoji="⏹️", style=discord.ButtonStyle.danger, row=2, custom_id="music_stop")
    async def stop(self, interaction: discord.Interaction, _):
        player = self.player or getattr(getattr(interaction, "guild", None), "voice_client", None)
        if not isinstance(player, MusicPlayer):
            return
        await interaction.response.defer()
        if player:
            if not await _require_same_voice_channel(interaction, player):
                return
            control_message = getattr(player, "control_message", None)
            if control_message is not None:
                try:
                    await control_message.delete()
                except Exception:
                    logger.exception("Failed to delete control message on stop")
                finally: player.control_message = None

            try:
                await player.disconnect()
            except LavalinkException:
                logger.exception("Lavalink refused disconnect on stop")
            
            try:
                player.client.dispatch("music_stopped")
            except Exception:
                logger.exception("Failed to dispatch music_stopped")

    @discord.ui.button(label="Инфо", emoji="ℹ️", style=discord.ButtonStyle.secondary, row=2, custom_id="music_info")
    async def info(self, interaction: discord.Interaction, _):
        player = self.player or getattr(getattr(interaction, "guild", None), "voice_client", None)
        if not isinstance(player, MusicPlayer):
            return
        await interaction.response.defer(ephemeral=True)
        if not await _require_same_voice_channel(interaction, player):
            return
        await interaction.followup.send(embed=build_embed(player), ephemeral=True)

# ================= START TRACK =================

def get_music_controls(player: MusicPlayer) -> MusicControls:
    view = getattr(player, "control_view", None)
    if isinstance(view, MusicControls):
        return view
    view = MusicControls(player)
    player.control_view = view
    return view


async def start_track(player: MusicPlayer, track, auto: bool):
    player.current_track = track
    player.track_start_time = time.time()

    try:
        await player.play(track)
    except Exception as e:
        logger.exception("Ошибка запуска трека: %s", e)
        return

    try:
        if player.volume != 100:
            await player.set_volume(100)
    except Exception:
        logger.exception("Не удалось установить громкость плеера")

    if player.control_message:
        await safe_message_edit(
            player.control_message,
            embed=build_embed(player),
            view=get_music_controls(player)
        )

# ================= CONTROL MESSAGE =================

async def send_control_message(interaction: discord.Interaction, player: MusicPlayer):
    embed = discord.Embed(
        title="🎵 Музыка",
        description="Загрузка...",
        color=0x57F287,
    )
    view = get_music_controls(player)
    message = await interaction.followup.send(
        embed=embed,
        view=view
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
    kwargs = {
        "content": content,
        "embed": embed,
        "wait": True,
    }
    if view is not None:
        kwargs["view"] = view

    message = await interaction.followup.send(**kwargs)

    async def _delete_later():
        try:
            await asyncio.sleep(delete_after)
            if not message.flags.ephemeral:
                await message.delete()
        except asyncio.CancelledError:
            return
        except discord.NotFound:
            return
    asyncio.create_task(_delete_later())
    return message
