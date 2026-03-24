import discord
import random
import json
import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from discord import app_commands
from discord.ext import commands
from edit_guard import safe_message_edit

logger = logging.getLogger(__name__)
data_lock = asyncio.Lock()

# ================= CONFIG =================

DATE_FORMAT = "%d.%m.%Y %H:%M"
MOSCOW_TZ = timezone(timedelta(hours=3))

# Admin role from environment
ADMIN_ROLE_ID_STR = os.getenv("GIVEAWAY_ADMIN_ROLE_ID")
ADMIN_ROLE_ID = int(ADMIN_ROLE_ID_STR) if ADMIN_ROLE_ID_STR and ADMIN_ROLE_ID_STR.isdigit() else None
OWNER_ID_STR = os.getenv("OWNER_ID", "")
OWNER_ID = int(OWNER_ID_STR) if OWNER_ID_STR.isdigit() else None

GIVEAWAY_FILE = Path("giveaways.json")

if not GIVEAWAY_FILE.exists():
    GIVEAWAY_FILE.write_text("{}", encoding="utf-8")


# ================= DATA =================

def load_data():
    try:
        return json.loads(GIVEAWAY_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Не удалось прочитать giveaways.json")
        return {}


def save_data(data):
    GIVEAWAY_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ================= TIME =================

def get_end_utc(end_date: str):
    end_msk = datetime.strptime(end_date, DATE_FORMAT).replace(tzinfo=MOSCOW_TZ)
    return end_msk.astimezone(timezone.utc)


def format_time_left(end_date: str):
    end_utc = get_end_utc(end_date)
    now = datetime.now(timezone.utc)

    if now >= end_utc:
        return "Розыгрыш завершён"

    diff = end_utc - now
    seconds = int(diff.total_seconds())

    days = seconds // 86400
    hours = seconds // 3600
    minutes = seconds // 60

    if days > 0:
        return f"Осталось {days} д."
    if hours > 0:
        return f"Осталось {hours} ч."
    if minutes > 0:
        return f"Осталось {minutes} мин."
    return "Менее минуты"


# ================= EMBEDS =================

def build_giveaway_embed(giveaway, guild):
    end_dt = datetime.strptime(giveaway["end_date"], DATE_FORMAT)

    embed = discord.Embed(
        title="🎉 РОЗЫГРЫШ",
        description=f"## {giveaway['title']}\n{giveaway['description']}",
        color=0xF1C40F if not giveaway["ended"] else 0x95A5A6
    )

    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━",
        value=(
            f"👥 **Участники:** `{len(giveaway['participants'])}`\n"
            f"🏆 **Победителей:** `{giveaway['winners']}`"
        ),
        inline=False
    )

    embed.add_field(
        name="⏳ Завершение",
        value=(
            f"`{end_dt.strftime('%d.%m.%Y %H:%M')}`\n"
            f"**{format_time_left(giveaway['end_date'])}**"
        ),
        inline=False
    )

    if giveaway["allowed_roles"]:
        roles = [guild.get_role(r) for r in giveaway["allowed_roles"]]
        mentions = " ".join(role.mention for role in roles if role)
        embed.add_field(name="🎭 Участвуют роли", value=mentions, inline=False)

    if giveaway.get("image"):
        embed.set_image(url=giveaway["image"])

    embed.set_footer(
        text="Розыгрыш завершён" if giveaway["ended"]
        else "Нажмите кнопку ниже, чтобы участвовать"
    )

    return embed


def build_winners_embed(giveaway):
    embed = discord.Embed(
        title="🏆 РЕЗУЛЬТАТЫ РОЗЫГРЫША",
        description=f"## {giveaway['title']}",
        color=0x2ECC71
    )

    embed.add_field(
        name="🎁 Победители",
        value=giveaway["result_text"],
        inline=False
    )

    embed.set_footer(text="Спасибо всем за участие 🎉")
    return embed


# ================= VIEW =================

class GiveawayView(discord.ui.View):
    def __init__(self, disabled=False):
        super().__init__(timeout=None)

        self.join_button = discord.ui.Button(
            label="🎉 Участвовать",
            style=discord.ButtonStyle.success,
            custom_id="giveaway_join_button",
            disabled=disabled
        )
        self.join_button.callback = self.join
        self.add_item(self.join_button)

    async def join(self, interaction: discord.Interaction):
        message_id = str(interaction.message.id)
        async with data_lock:
            data = load_data()
            giveaway = data.get(message_id)

            if not giveaway or giveaway["ended"]:
                await interaction.response.send_message("❌ Розыгрыш завершён.", ephemeral=True)
                return

            if giveaway["allowed_roles"]:
                if not any(r.id in giveaway["allowed_roles"] for r in interaction.user.roles):
                    await interaction.response.send_message("❌ У тебя нет роли для участия.", ephemeral=True)
                    return

            if interaction.user.id in giveaway["participants"]:
                await interaction.response.send_message("⚠ Ты уже участвуешь.", ephemeral=True)
                return

            giveaway["participants"].append(interaction.user.id)
            save_data(data)

        await safe_message_edit(
            interaction.message,
            embed=build_giveaway_embed(giveaway, interaction.guild),
            view=self
        )

        await interaction.response.send_message("✅ Ты участвуешь!", ephemeral=True)


# ================= COG =================

class Giveaway(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(GiveawayView())
        self.bot.loop.create_task(self.restore_giveaways())

    def has_admin_role(self, member):
        if ADMIN_ROLE_ID is None:
            return OWNER_ID is not None and member.id == OWNER_ID
        return ADMIN_ROLE_ID in [r.id for r in member.roles]

    async def restore_giveaways(self):
        await self.bot.wait_until_ready()
        data = load_data()

        for message_id, giveaway in data.items():
            if giveaway.get("ended"):
                continue

            self.bot.loop.create_task(self.schedule_end(message_id))
            self.bot.loop.create_task(self.update_loop(message_id))

    # ================= SAFE UPDATE LOOP =================

    async def update_loop(self, message_id):
        while True:
            await asyncio.sleep(60)

            data = load_data()
            giveaway = data.get(message_id)

            if not giveaway or giveaway.get("ended"):
                return

            channel = self.bot.get_channel(giveaway["channel_id"])
            if not channel:
                return

            try:
                message = await channel.fetch_message(int(message_id))
                await safe_message_edit(message,
                    embed=build_giveaway_embed(giveaway, channel.guild),
                    view=GiveawayView(disabled=False)
                )
            except Exception:
                logger.exception("Не удалось обновить giveaway %s", message_id)
                return

    # ================= SAFE SCHEDULE END =================

    async def schedule_end(self, message_id):
        data = load_data()
        giveaway = data.get(message_id)

        if not giveaway or giveaway.get("ended"):
            return

        delay = (
            get_end_utc(giveaway["end_date"])
            - datetime.now(timezone.utc)
        ).total_seconds()

        if delay > 0:
            await asyncio.sleep(delay)

        await self.finish_giveaway(message_id)

    # ================= SAFE FINISH =================

    async def finish_giveaway(self, message_id):
        async with data_lock:
            data = load_data()
            giveaway = data.get(message_id)

            if not giveaway or giveaway.get("ended"):
                return

            participants = giveaway["participants"]

            winners = random.sample(
                participants,
                min(len(participants), giveaway["winners"])
            ) if participants else []

            result_text = (
                "\n".join(f"👑 <@{uid}>" for uid in winners)
                if winners else "Нет участников"
            )

            giveaway["ended"] = True
            giveaway["result_text"] = result_text
            save_data(data)

        channel = self.bot.get_channel(giveaway["channel_id"])
        if not channel:
            return

        try:
            message = await channel.fetch_message(int(message_id))
            await safe_message_edit(message,
                embed=build_giveaway_embed(giveaway, channel.guild),
                view=GiveawayView(disabled=True)
            )
        except Exception:
            logger.exception("Не удалось обновить завершенный giveaway %s", message_id)

        # -------- Объявление победителей --------

        if len(winners) == 1:
            title_text = "🎉 Поздравляем победителя!"
        else:
            title_text = "🎉 Поздравляем победителей!"

        role_ping = ""
        if giveaway["allowed_roles"]:
            roles = [
                channel.guild.get_role(r)
                for r in giveaway["allowed_roles"]
            ]
            role_ping = " ".join(role.mention for role in roles if role)

        winners_ping = " ".join(f"<@{uid}>" for uid in winners)

        try:
            await channel.send(
                content=f"{title_text}\n{role_ping}\n{winners_ping}".strip(),
                embed=build_winners_embed(giveaway)
            )
        except Exception:
            logger.exception("Не удалось отправить победителей giveaway %s", message_id)

    # ================= COMMANDS =================

    @app_commands.command(name="giveaway", description="Создать розыгрыш")
    @app_commands.describe(
        title="Название",
        description="Описание",
        winners="Количество победителей (1-10)",
        channel="Канал публикации",
        end_date="Дата окончания (24.02.2026 18:40)",
        allowed_role="Роль допуска (опционально)",
        image="Картинка (опционально)"
    )
    @app_commands.choices(
        winners=[app_commands.Choice(name=str(i), value=i) for i in range(1, 11)]
    )
    async def giveaway(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        winners: app_commands.Choice[int],
        channel: discord.TextChannel,
        end_date: str,
        allowed_role: discord.Role = None,
        image: discord.Attachment = None
    ):
        if not self.has_admin_role(interaction.user):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        try:
            datetime.strptime(end_date, DATE_FORMAT)
        except ValueError:
            await interaction.response.send_message("❌ Формат даты: 24.02.2026 18:40", ephemeral=True)
            return

        giveaway_data = {
            "title": title,
            "description": description,
            "winners": winners.value,
            "channel_id": channel.id,
            "participants": [],
            "end_date": end_date,
            "allowed_roles": [allowed_role.id] if allowed_role else [],
            "image": image.url if image else None,
            "ended": False
        }

        embed = build_giveaway_embed(giveaway_data, interaction.guild)
        message = await channel.send(embed=embed, view=GiveawayView())

        async with data_lock:
            data = load_data()
            data[str(message.id)] = giveaway_data
            save_data(data)

        self.bot.loop.create_task(self.schedule_end(str(message.id)))
        self.bot.loop.create_task(self.update_loop(str(message.id)))

        await interaction.response.send_message("✅ Розыгрыш создан!", ephemeral=True)

    @app_commands.command(name="giveaway_remove", description="Исключить участника")
    async def giveaway_remove(
        self,
        interaction: discord.Interaction,
        message_id: str,
        user: discord.User
    ):
        if not self.has_admin_role(interaction.user):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        async with data_lock:
            data = load_data()
            giveaway = data.get(message_id)

            if not giveaway:
                await interaction.response.send_message("❌ Не найден.", ephemeral=True)
                return

            if user.id not in giveaway["participants"]:
                await interaction.response.send_message("⚠ Пользователь не участвует.", ephemeral=True)
                return

            giveaway["participants"].remove(user.id)
            save_data(data)

        channel = self.bot.get_channel(giveaway["channel_id"])

        try:
            message = await channel.fetch_message(int(message_id))
            await safe_message_edit(message,
                embed=build_giveaway_embed(giveaway, channel.guild),
                view=GiveawayView(disabled=giveaway["ended"])
            )
        except Exception:
            logger.exception("Не удалось обновить giveaway после удаления участника %s", message_id)

        await interaction.response.send_message("✅ Участник исключён.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Giveaway(bot))
