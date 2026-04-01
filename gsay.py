import discord
import re
import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from edit_guard import safe_message_edit
from config import OWNER_ID

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ================= ENV =================

load_dotenv()
logger = logging.getLogger(__name__)

from web_config import get_web_config, get_int_list


def _get_allowed_role_ids() -> list[int]:
    cfg = get_web_config()
    ids = get_int_list(cfg, ["gsay", "allowed_roles"], default=None)
    if ids is not None:
        return ids

    raw = os.getenv("GSAY_ALLOWED_ROLES", "")
    out: list[int] = []
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
            else:
                logger.warning("Invalid role ID in GSAY_ALLOWED_ROLES: %s", part)
    return out

# ================= LOCATION CONFIG =================

LOCATION_CONFIG = {
    "Склад#3": {"color": 0xE67E22, "icon": "📦"},
    "Офис #1": {"color": 0x3498DB, "icon": "🏢"},
    "Дом": {"color": 0x2ECC71, "icon": "🏠"},
    "Особняк": {"color": 0x9B59B6, "icon": "🏰"},
}

# ================= TIME =================

def format_time_left(target_dt: datetime):
    now = datetime.now()

    if now >= target_dt:
        return "⛔ Время истекло"

    diff = target_dt - now
    seconds = int(diff.total_seconds())

    hours = seconds // 3600
    minutes = seconds // 60

    if hours > 0:
        return f"⏳ Осталось {hours} ч."
    if minutes > 0:
        return f"⏳ Осталось {minutes} мин."
    return "⏳ Менее минуты"


# ================= COG =================

class GSay(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Проверка доступа ----------
    def has_permission(self, member: discord.Member) -> bool:
        if member.id == OWNER_ID:
            return True
        allowed_role_ids = set(_get_allowed_role_ids())
        return any(r.id in allowed_role_ids for r in member.roles)

    # ---------- Построение Embed ----------
    def build_embed(self, text, location, group_code, time, target_time, image):
        color = 0x5865F2
        location_icon = "📍"

        if location:
            config = LOCATION_CONFIG.get(location)
            if config:
                color = config["color"]
                location_icon = config["icon"]

        embed = discord.Embed(
            title="📢",
            description=f"**{text}**",
            color=color
        )

        embed.add_field(name="━━━━━━━━━━━━━━━━━━", value="", inline=False)

        if location:
            embed.add_field(
                name=f"{location_icon} ЛОКАЦИЯ",
                value=f"**{location}**",
                inline=True
            )

        if group_code:
            embed.add_field(
                name="🆔 КОД",
                value=f"**{group_code}**",
                inline=True
            )

        embed.add_field(
            name="🕒 ВРЕМЯ",
            value=f"`{time}`\n{format_time_left(target_time)}",
            inline=False
        )

        if image:
            embed.set_image(url=image.url)

        embed.set_footer(text="☠️ DECEASED ☠️")

        return embed

    # ================= COMMAND =================

    @app_commands.command(
        name="gsay",
        description="Отправить сообщение от имени бота"
    )
    @app_commands.describe(
        channel="Канал, куда будет отправлено сообщение",
        text="Основной текст сообщения",
        role="Роль для упоминания (опционально)",
        location="Выбор локации",
        group_code="Код группы (до 5 символов A-Z 0-9)",
        time="Время в формате XX:XX",
        image="Картинка (опционально)",
        repeat="Повторить сообщение (1-3 раза)"
    )
    @app_commands.choices(
        location=[
            app_commands.Choice(name="Склад#3", value="Склад#3"),
            app_commands.Choice(name="Офис #1", value="Офис #1"),
            app_commands.Choice(name="Дом", value="Дом"),
            app_commands.Choice(name="Особняк", value="Особняк"),
        ],
        repeat=[
            app_commands.Choice(name="1", value=1),
            app_commands.Choice(name="2", value=2),
            app_commands.Choice(name="3", value=3),
        ]
    )
    @app_commands.guild_only()
    async def gsay(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        text: str,
        time: str,
        role: Optional[discord.Role] = None,
        location: Optional[app_commands.Choice[str]] = None,
        group_code: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
        repeat: Optional[app_commands.Choice[int]] = None
    ):

        await interaction.response.defer(ephemeral=True)

        # 🔐 Проверка доступа
        if not self.has_permission(interaction.user):
            await interaction.followup.send("❌ Нет доступа.", ephemeral=True)
            return

        # 🕒 Проверка времени
        if not re.match(r"^\d{2}:\d{2}$", time):
            await interaction.followup.send("❌ Формат времени XX:XX", ephemeral=True)
            return

        hours, minutes = map(int, time.split(":"))
        if hours > 23 or minutes > 59:
            await interaction.followup.send("❌ Время должно быть в диапазоне 00:00-23:59", ephemeral=True)
            return

        now = datetime.now()
        target_time = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)

        if target_time <= now:
            target_time += timedelta(days=1)

        # 🆔 Проверка кода группы
        if group_code:
            if not re.match(r"^[A-Za-z0-9]{1,5}$", group_code):
                await interaction.followup.send(
                    "❌ Код группы до 5 символов (A-Z 0-9)",
                    ephemeral=True
                )
                return
            group_code = group_code.upper()

        location_value = location.value if location else ""
        total_messages = repeat.value if repeat else 1
        content = role.mention if role else ""

        current_message: Optional[discord.Message] = None

        # ---------- Таймер ----------
        async def updater():
            nonlocal current_message

            while True:
                await asyncio.sleep(60)

                if not current_message:
                    return

                # Stop updater once target time has passed
                if datetime.now() >= target_time:
                    try:
                        await safe_message_edit(current_message,
                            embed=self.build_embed(
                                text,
                                location_value,
                                group_code,
                                time,
                                target_time,
                                image
                            )
                        )
                    except Exception:
                        logger.exception("Не удалось обновить финальное gsay сообщение")
                    return

                try:
                    await safe_message_edit(current_message,
                        embed=self.build_embed(
                            text,
                            location_value,
                            group_code,
                            time,
                            target_time,
                            image
                        )
                    )
                except Exception:
                    logger.exception("Не удалось обновить gsay сообщение")
                    return

        # ---------- Отправка ----------
        async def send_new():
            nonlocal current_message
            embed = self.build_embed(
                text,
                location_value,
                group_code,
                time,
                target_time,
                image
            )
            current_message = await channel.send(content=content, embed=embed)

        await send_new()

        # запускаем updater
        asyncio.create_task(updater())

        # ---------- Повторы ----------
        if total_messages > 1:
            for _ in range(total_messages - 1):
                await asyncio.sleep(60)

                if current_message:
                    try:
                        await current_message.delete()
                    except Exception:
                        logger.exception("Не удалось удалить предыдущее gsay сообщение")

                await send_new()

        await interaction.followup.send(
            "✅ Сообщение отправлено.",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GSay(bot))
