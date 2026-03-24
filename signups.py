import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
import re
from edit_guard import safe_message_edit

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DATE_FORMAT = "%d.%m.%Y %H:%M"
MOSCOW_TZ = timezone(timedelta(hours=3))
data_lock = asyncio.Lock()

DATA_FILE = Path("signups.json")
if not DATA_FILE.exists():
    DATA_FILE.write_text("{}", encoding="utf-8")

SIGNUP_MANAGERS = [
    int(x.strip()) for x in os.getenv("SIGNUP_MANAGERS", "").split(",") if x.strip().isdigit()
]
SIGNUP_ADMINS = [
    int(x.strip()) for x in os.getenv("SIGNUP_ADMINS", "").split(",") if x.strip().isdigit()
]
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "0").isdigit() else 0

# ================= SAFE DATA =================

def load_data():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_data(data):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ================= TIME =================

def to_utc(dt: datetime):
    return dt.astimezone(timezone.utc)

def now_utc():
    return datetime.now(timezone.utc)

def parse_date(date_str: str):
    date_str = date_str.strip()
    dt = None

    try:
        dt = datetime.strptime(date_str, DATE_FORMAT)
        dt = dt.replace(tzinfo=MOSCOW_TZ)
    except ValueError:
        dt = None

    if not dt:
        try:
            time_part = datetime.strptime(date_str, "%H:%M").time()
            today = datetime.now(MOSCOW_TZ).date()
            dt = datetime.combine(today, time_part)
            dt = dt.replace(tzinfo=MOSCOW_TZ)
        except ValueError:
            return None

    if to_utc(dt) <= now_utc():
        return "past"

    return dt

# ================= ROLES =================

ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
ID_RE = re.compile(r"\d+")

def parse_roles_input(guild: discord.Guild, text: str):
    ids = set()
    for m in ROLE_MENTION_RE.finditer(text):
        ids.add(int(m.group(1)))
    for m in ID_RE.finditer(text):
        ids.add(int(m.group(0)))
    return list(ids)

# ================= EMBED =================

def format_time_left(end_dt):
    diff = to_utc(end_dt) - now_utc()
    if diff.total_seconds() <= 0:
        return "Сбор завершён"
    seconds = int(diff.total_seconds())
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"

def build_signup_embed(signup, guild):
    end_dt = parse_date(signup["end_date"])

    main = [f"<@{u['id']}>" for u in signup["participants"] if u["place"] == "main"]
    extra = [f"<@{u['id']}>" for u in signup["participants"] if u["place"] == "extra"]

    embed = discord.Embed(
        title="📋 ЗАПИСЬ",
        description=f"## {signup['title']}",
        color=0x5865F2 if not signup.get("closed") else 0x2F3136
    )

    if isinstance(end_dt, datetime):
        embed.add_field(
            name="🗓 Информация",
            value=(
                f"**Дата:** {end_dt.strftime('%d.%m.%Y')} | {end_dt.strftime('%H:%M')}\n"
                f"**Осталось:** {format_time_left(end_dt)}"
            ),
            inline=False
        )

    embed.add_field(
        name="🎯 Слоты",
        value=(
            f"Основные: **{len(main)} / {signup['slots']}**\n"
            f"Резерв: **{len(extra)} / {signup.get('extra_slots',0)}**"
        ),
        inline=False
    )

    embed.add_field(
        name=f"👥 Участники ({len(main)})",
        value="\n".join(main) if main else "—",
        inline=False
    )

    if signup.get("extra_slots", 0) > 0:
        embed.add_field(
            name=f"💤 Резерв ({len(extra)})",
            value="\n".join(extra) if extra else "—",
            inline=False
        )

    if signup.get("thread_url"):
        embed.add_field(
            name="🧵 Обсуждение",
            value=f"[Перейти к ветке]({signup['thread_url']})",
            inline=False
        )

    embed.set_footer(
        text="Запись закрыта" if signup.get("closed")
        else "Нажмите «Присоединиться», чтобы записаться"
    )

    if signup.get("image"):
        embed.set_image(url=signup["image"])

    return embed

# ================= VIEW =================

class SignupView(discord.ui.View):

    def __init__(self, bot, message_id=None, disabled=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.message_id = message_id

        if disabled:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.primary, custom_id="signup_join")
    async def join(self, interaction: discord.Interaction, button):

        await interaction.response.defer(ephemeral=True)
        async with data_lock:
            data = load_data()
            sign = data.get(str(interaction.message.id))

            if not sign or sign.get("closed"):
                await interaction.followup.send("❌ Запись закрыта.", ephemeral=True)
                return

            if any(u["id"] == interaction.user.id for u in sign["participants"]):
                await interaction.followup.send("⚠ Вы уже в списке.", ephemeral=True)
                return

            main_count = sum(1 for u in sign["participants"] if u["place"] == "main")

            if main_count < sign["slots"]:
                place = "main"
            else:
                extra_count = sum(1 for u in sign["participants"] if u["place"] == "extra")
                if extra_count < sign.get("extra_slots", 0):
                    place = "extra"
                else:
                    await interaction.followup.send("⛔ Слоты заняты.", ephemeral=True)
                    return

            sign["participants"].append({"id": interaction.user.id, "place": place})
            save_data(data)

        if sign.get("thread_id"):
            thread = interaction.guild.get_thread(sign["thread_id"])
            if thread:
                try:
                    await thread.add_user(interaction.user)
                except Exception:
                    logger.exception("Не удалось добавить пользователя в thread signup")

        channel = self.bot.get_channel(sign["channel_id"])
        if not channel:
            await interaction.followup.send("❌ Канал записи не найден.", ephemeral=True)
            return
        msg = await channel.fetch_message(int(interaction.message.id))

        await safe_message_edit(
            msg,
            embed=build_signup_embed(sign, channel.guild),
            view=SignupView(self.bot, interaction.message.id)
        )

        await interaction.followup.send("✅ Вы записаны.", ephemeral=True)

    @discord.ui.button(label="Покинуть", style=discord.ButtonStyle.danger, custom_id="signup_leave")
    async def leave(self, interaction: discord.Interaction, button):

        await interaction.response.defer(ephemeral=True)
        async with data_lock:
            data = load_data()
            sign = data.get(str(interaction.message.id))

            if not sign or sign.get("closed"):
                await interaction.followup.send("❌ Сбор уже закрыт.", ephemeral=True)
                return

            sign["participants"] = [
                u for u in sign["participants"] if u["id"] != interaction.user.id
            ]

            save_data(data)

        channel = self.bot.get_channel(sign["channel_id"])
        if not channel:
            await interaction.followup.send("❌ Канал записи не найден.", ephemeral=True)
            return
        msg = await channel.fetch_message(int(interaction.message.id))

        await safe_message_edit(
            msg,
            embed=build_signup_embed(sign, channel.guild),
            view=SignupView(self.bot, interaction.message.id)
        )

        await interaction.followup.send("✅ Вы покинули список.", ephemeral=True)

# ================= COG =================

class Signups(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(SignupView(bot))
        self.bot.loop.create_task(self.restore_signups())

    def has_permission(self, member: discord.Member) -> bool:
        if member.id == OWNER_ID:
            return True
        allowed_ids = set(SIGNUP_MANAGERS) | set(SIGNUP_ADMINS)
        return any(role.id in allowed_ids for role in member.roles)

    async def restore_signups(self):
        await self.bot.wait_until_ready()
        data = load_data()

        for msg_id, sign in data.items():

            channel = self.bot.get_channel(sign["channel_id"])
            if not channel:
                continue

            try:
                message = await channel.fetch_message(int(msg_id))
            except Exception:
                logger.exception("Не удалось получить signup сообщение %s", msg_id)
                continue

            try:
                await safe_message_edit(
                    message,
                    embed=build_signup_embed(sign, channel.guild),
                    view=SignupView(
                        self.bot,
                        msg_id,
                        disabled=sign.get("closed")
                    )
                )
            except Exception:
                logger.exception("Не удалось восстановить signup %s", msg_id)
                continue

            if not sign.get("closed"):
                self.bot.loop.create_task(self.schedule_end(msg_id))

    async def schedule_end(self, msg_id):

        async with data_lock:
            data = load_data()
            sign = data.get(msg_id)

            if not sign or sign.get("closed"):
                return

        end_dt = parse_date(sign["end_date"])

        if isinstance(end_dt, str) or end_dt is None:
            return

        delay = (to_utc(end_dt) - now_utc()).total_seconds()

        if delay > 0:
            await asyncio.sleep(delay)

        async with data_lock:
            data = load_data()
            sign = data.get(msg_id)

            if not sign or sign.get("closed"):
                return

            sign["closed"] = True
            save_data(data)

        channel = self.bot.get_channel(sign["channel_id"])
        if not channel:
            return

        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception:
            logger.exception("Не удалось получить signup сообщение для завершения %s", msg_id)
            return

        try:
            await safe_message_edit(
                msg,
                embed=build_signup_embed(sign, channel.guild),
                view=SignupView(self.bot, msg_id, disabled=True)
            )
        except Exception:
            logger.exception("Не удалось закрыть signup %s", msg_id)

    # ================= COMMAND =================

    @app_commands.command(name="plus", description="Создать список записи")
    async def plus(self,
                   interaction: discord.Interaction,
                   title: str,
                   end_date: str,
                   slots: int,
                   extra_slots: int | None = None,
                   image: discord.Attachment | None = None,
                   branch: str | None = None,
                   roles: str = ""):

        await interaction.response.defer(ephemeral=True)

        if not self.has_permission(interaction.user):
            await interaction.followup.send("❌ Нет доступа.", ephemeral=True)
            return

        if slots <= 0 or (extra_slots is not None and extra_slots < 0):
            await interaction.followup.send("❌ Количество слотов указано неверно.", ephemeral=True)
            return

        dt = parse_date(end_date)

        if dt == "past":
            await interaction.followup.send("❌ Указанное время уже прошло.", ephemeral=True)
            return

        if not dt:
            await interaction.followup.send("❌ Формат: 18:30 или 24.02.2026 18:30", ephemeral=True)
            return

        roles_ids = parse_roles_input(interaction.guild, roles)

        embed = discord.Embed(
            title="📋 ЗАПИСЬ",
            description=f"## {title}",
            color=0x5865F2
        )

        message = await interaction.channel.send(embed=embed)

        thread_id = None
        thread_url = None

        if branch:

            thread = await message.create_thread(
                name=branch,
                auto_archive_duration=1440
            )

            thread_id = thread.id
            thread_url = f"https://discord.com/channels/{interaction.guild.id}/{thread.id}"

            try:
                await thread.add_user(interaction.user)
            except Exception:
                logger.exception("Не удалось добавить автора в ветку signup")

        if roles_ids:

            roles_text = " ".join(f"<@&{rid}>" for rid in roles_ids)

            tag_msg = await interaction.channel.send(roles_text)

            async def delete_tag():
                await asyncio.sleep(30)
                try:
                    await tag_msg.delete()
                except Exception:
                    logger.exception("Не удалось удалить временный tag signup")

            asyncio.create_task(delete_tag())

        signup = {
            "title": title,
            "end_date": end_date,
            "slots": slots,
            "extra_slots": extra_slots or 0,
            "image": image.url if image else None,
            "branch": branch,
            "roles_to_ping": roles_ids,
            "participants": [],
            "channel_id": interaction.channel.id,
            "closed": False,
            "thread_id": thread_id,
            "thread_url": thread_url
        }

        embed = build_signup_embed(signup, interaction.guild)

        await safe_message_edit(
            message,
            embed=embed,
            view=SignupView(self.bot, message.id)
        )

        async with data_lock:
            data = load_data()
            data[str(message.id)] = signup
            save_data(data)

        self.bot.loop.create_task(self.schedule_end(str(message.id)))

        await interaction.followup.send("✅ Список создан.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Signups(bot))
