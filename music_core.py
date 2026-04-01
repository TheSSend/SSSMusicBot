import discord
import wavelink
import asyncio
import time
import logging
import aiohttp

from wavelink import LavalinkException

from edit_guard import safe_message_edit

logger = logging.getLogger(__name__)

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

        queue_encoded = [t.encoded for t in player.queue]

        # Handle position calculation
        pos = getattr(player, "position", 0)  # sometimes provided by wavelink player object
        if pos == 0 and player.track_start_time:
            pos = int((time.time() - player.track_start_time) * 1000)

        pd = {
            "guild_id": player.guild.id,
            "channel_id": getattr(player.channel, "id", 0),
            "text_channel_id": getattr(player.control_message.channel, "id", 0) if player.control_message else 0,
            "track_encoded": player.current_track.encoded,
            "position": pos,
            "queue_encoded": queue_encoded,
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
    def __init__(self, player: MusicPlayer):
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
        if not self.player:
            return
        if not await _require_same_voice_channel(interaction, self.player):
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
            await self.player.set_filters(filters)
            # Re-update the select text visually? Only if we wanted dynamic updating
            if self.player.control_message:
                 await safe_message_edit(
                    self.player.control_message,
                    embed=build_embed(self.player),
                    view=MusicControls(self.player),
                )
        except Exception:
             logger.exception("Failed to apply Audio Filters")


# ================= CONTROLS =================

class MusicControls(discord.ui.View):
    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player
        
        # Add the Select Menu directly to the view
        self.add_item(FiltersSelect(player))

    @discord.ui.button(label="Пауза", emoji="⏸️", style=discord.ButtonStyle.secondary, row=1)
    async def pause(self, interaction: discord.Interaction, _):
        if not self.player: return
        await interaction.response.defer()
        if not await _require_same_voice_channel(interaction, self.player):
            return

        try:
            await self.player.pause(not self.player.paused)
        except LavalinkException:
            try:
                await self.player.disconnect()
            except Exception:
                logger.exception("Failed to disconnect player after pause error")
        else:
            if self.player.control_message:
                try:
                    await safe_message_edit(
                        self.player.control_message,
                        embed=build_embed(self.player),
                        view=MusicControls(self.player),
                    )
                except Exception:
                    logger.exception("Failed to refresh control message after pause")

    @discord.ui.button(label="Следующий", emoji="⏭️", style=discord.ButtonStyle.primary, row=1)
    async def skip(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        if self.player:
            if not await _require_same_voice_channel(interaction, self.player):
                return
            if self.player.queue.is_empty:
                await interaction.followup.send("📭 Очередь пуста", ephemeral=True)
                return
            try:
                await self.player.stop()
            except Exception:
                logger.exception("Failed to skip track")

    @discord.ui.button(label="Очередь", emoji="📜", style=discord.ButtonStyle.success, row=1)
    async def queue(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        if not self.player or not await _require_same_voice_channel(interaction, self.player):
            return
        if self.player.queue.is_empty:
            await interaction.followup.send("📭 Очередь пуста", ephemeral=True)
            return
        text = "\n".join(
            f"{i+1}. {t.title}"
            for i, t in enumerate(list(self.player.queue)[:10])
        )
        if len(self.player.queue) > 10:
            text += f"\n...и еще {len(self.player.queue) - 10} треков"
        await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Текст", emoji="🎤", style=discord.ButtonStyle.secondary, row=2)
    async def lyrics(self, interaction: discord.Interaction, _):
        """Fetch lyrics from public API"""
        await interaction.response.defer(ephemeral=True)
        
        if not self.player or not self.player.current_track:
            await interaction.followup.send("❌ Нет играющего трека.", ephemeral=True)
            return

        if not await _require_same_voice_channel(interaction, self.player):
            return

        track_title = self.player.current_track.title
        author = self.player.current_track.author

        clean_title = " ".join(str(track_title).split()).strip()
        clean_author = " ".join(str(author).split()).strip()
        for suffix in (" - Topic", " (Official Video)", " (Official Music Video)", " (Lyrics)", " Lyrics"):
            if clean_title.endswith(suffix):
                clean_title = clean_title[: -len(suffix)].strip()
        if " - " in clean_title:
            left, right = [part.strip() for part in clean_title.split(" - ", 1)]
            if left and right:
                clean_title, clean_author = right, left

        headers = {
            "User-Agent": "Musicbot/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for title_value, artist_value in (
                    (clean_title, clean_author),
                    (track_title, clean_author),
                    (clean_title, author),
                ):
                    if not title_value or not artist_value:
                        continue

                    params = {
                        "track_name": title_value,
                        "artist_name": artist_value,
                    }
                    if getattr(self.player.current_track, "album", None):
                        params["album_name"] = getattr(self.player.current_track, "album")
                    if getattr(self.player.current_track, "length", None):
                        params["duration"] = int(getattr(self.player.current_track, "length", 0) or 0) // 1000

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
                                    await interaction.followup.send(embed=embed, ephemeral=True)
                                    return
                    except Exception:
                        pass
        except Exception:
            logger.exception("Failed to fetch lyrics from fallback services")

        normalized = track_title.replace(" - Topic", "").replace(" - Official Music Video", "")
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


    @discord.ui.button(label="Стоп", emoji="⏹️", style=discord.ButtonStyle.danger, row=2)
    async def stop(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        if self.player:
            if not await _require_same_voice_channel(interaction, self.player):
                return
            control_message = getattr(self.player, "control_message", None)
            if control_message is not None:
                try:
                    await control_message.delete()
                except Exception:
                    logger.exception("Failed to delete control message on stop")
                finally: self.player.control_message = None

            try:
                await self.player.disconnect()
            except LavalinkException:
                logger.exception("Lavalink refused disconnect on stop")
            
            try:
                self.player.client.dispatch("music_stopped")
            except Exception:
                logger.exception("Failed to dispatch music_stopped")

    @discord.ui.button(label="Инфо", emoji="ℹ️", style=discord.ButtonStyle.secondary, row=2)
    async def info(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        if not self.player or not await _require_same_voice_channel(interaction, self.player):
            return
        await interaction.followup.send(embed=build_embed(self.player), ephemeral=True)

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
        if player.volume != 100:
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
        color=0x57F287,
    )
    message = await interaction.followup.send(
        embed=embed,
        view=MusicControls(player)
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
