import discord
import json
import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from runtime_paths import data_path

load_dotenv()
logger = logging.getLogger(__name__)
data_lock = asyncio.Lock()

# ================= ENV =================

HR_ACCESS = [
    int(r.strip())
    for r in os.getenv("HR_ACCESS", "").split(",")
    if r.strip().isdigit()
]
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "0").isdigit() else 0

# ================= CALL CHANNELS =================

CALL_CHANNELS = [
    int(channel_id.strip())
    for channel_id in os.getenv("FAMILY_CALL_CHANNELS", "").split(",")
    if channel_id.strip().isdigit()
]

LOG_CHANNEL_ID = int(os.getenv("FAMILY_LOG_CHANNEL", "0"))
REMOVE_ROLE_ID = int(os.getenv("FAMILY_REMOVE_ROLE_ID", "0"))
ADD_ROLE_1_ID = int(os.getenv("FAMILY_ADD_ROLE_1_ID", "0"))
ADD_ROLE_2_ID = int(os.getenv("FAMILY_ADD_ROLE_2_ID", "0"))

# ================= TIME =================

MOSCOW_TZ = timezone(timedelta(hours=3))
DATE_FORMAT = "%d.%m.%Y %H:%M"

# ================= FILE =================

DATA_FILE = data_path("family_applications.json")

if not DATA_FILE.exists():
    DATA_FILE.write_text("{}", encoding="utf-8")


def load_data():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Не удалось прочитать family_applications.json")
        return {}


def save_data(data):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def now_time():
    return datetime.now(MOSCOW_TZ).strftime(DATE_FORMAT)


def has_hr_access(member: discord.Member):
    if member.id == OWNER_ID:
        return True

    allowed_ids = set(HR_ACCESS)
    if member.id in allowed_ids:
        return True

    return any(role.id in allowed_ids for role in member.roles)


# ================= MODAL =================

class JoinFamilyModal(discord.ui.Modal):

    nickname = discord.ui.TextInput(
        label="Ваш ник в игре",
        placeholder="Игровой ник: Имя Фамилия",
        required=True,
        min_length=3,
        max_length=100
    )

    static = discord.ui.TextInput(
        label="Статик #",
        placeholder="В правом верхнем углу, цифры после #",
        required=True,
        min_length=1,
        max_length=50
    )

    age = discord.ui.TextInput(
        label="Возраст",
        placeholder="Укажите полный возраст",
        required=True,
        min_length=1,
        max_length=3
    )

    goal = discord.ui.TextInput(
        label="Цель вступления",
        placeholder="Почему хотите вступить?",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=10,
        max_length=500
    )

    about = discord.ui.TextInput(
        label="Как узнали о семье",
        placeholder="Источник информации",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=5,
        max_length=500
    )

    def __init__(self, bot):
        super().__init__(title="Подать заявку")
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):

        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "❌ Команда доступна только на сервере.",
                ephemeral=True
            )
            return

        if CALL_CHANNELS and interaction.channel.id not in CALL_CHANNELS:
            await interaction.response.send_message(
                "❌ Подавать заявку можно только в специальных каналах.",
                ephemeral=True
            )
            return

        async with data_lock:
            data = load_data()
            guild_data = data.setdefault(str(interaction.guild.id), {})
            user_apps = guild_data.setdefault(str(interaction.user.id), [])
            previous_apps = list(user_apps)

            for app in user_apps:
                if not app["closed"]:
                    await interaction.response.send_message(
                        "❌ У вас уже есть активная заявка.",
                        ephemeral=True
                    )
                    return

        category = interaction.channel.category

        # ПРАВА ДОСТУПА
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }

        for role_id in HR_ACCESS:
            role = interaction.guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True
                )

        try:
            channel = await interaction.guild.create_text_channel(
                name=f"заявка-{interaction.user.name}".lower(),
                category=category,
                overwrites=overwrites
            )
        except Exception:
            logger.exception("Не удалось создать канал заявки в семью")
            await interaction.response.send_message(
                "❌ Не удалось создать канал заявки. Проверь права бота.",
                ephemeral=True
            )
            return

        application = {
            "nickname": self.nickname.value,
            "static": self.static.value,
            "age": self.age.value,
            "goal": self.goal.value,
            "about": self.about.value,
            "created_at": now_time(),
            "closed": False,
            "status": "open",
            "channel_id": channel.id,
            "log_message_id": None
        }

        async with data_lock:
            data = load_data()
            guild_data = data.setdefault(str(interaction.guild.id), {})
            user_apps = guild_data.setdefault(str(interaction.user.id), [])
            user_apps.append(application)
            save_data(data)

        # ================= ПРЕДЫДУЩИЕ ЗАЯВКИ =================

        if previous_apps:
            links = []

            for old in previous_apps:

                emoji = "🟡"

                if old.get("status") == "принята":
                    emoji = "🟢"
                elif old.get("status") == "отклонена":
                    emoji = "🔴"

                if old.get("log_message_id") and LOG_CHANNEL_ID:
                    link = f"https://discord.com/channels/{interaction.guild.id}/{LOG_CHANNEL_ID}/{old['log_message_id']}"
                    links.append(f"{emoji} [Заявка от {old['created_at']}]({link})")

                elif old.get("channel_id"):
                    link = f"https://discord.com/channels/{interaction.guild.id}/{old['channel_id']}"
                    links.append(f"{emoji} [Заявка от {old['created_at']}]({link})")

            if links:
                embed_prev = discord.Embed(
                    title="📂 Предыдущие заявки",
                    description="\n".join(links),
                    color=0x95A5A6
                )
                await channel.send(embed=embed_prev)

        # ================= ОСНОВНОЙ EMBED =================

        embed = discord.Embed(
            title="Заявление",
            color=0x9B59B6
        )

        embed.add_field(name="Ваш ник в игре", value=self.nickname.value, inline=False)
        embed.add_field(name="Статик #", value=self.static.value, inline=False)
        embed.add_field(name="Возраст", value=self.age.value, inline=False)
        embed.add_field(name="Цель вступления", value=self.goal.value, inline=False)
        embed.add_field(name="Как узнали о семье", value=self.about.value, inline=False)

        embed.add_field(name="Пользователь", value=interaction.user.mention, inline=False)
        embed.add_field(name="Username", value=interaction.user.name, inline=True)
        embed.add_field(name="ID", value=str(interaction.user.id), inline=True)

        embed.set_footer(text=now_time())

        await channel.send(
            embed=embed,
            view=ApplicationManageView(self.bot, interaction.user.id)
        )

        # УВЕДОМЛЕНИЕ HR
        hr_mentions = []
        for role_id in HR_ACCESS:
            role = interaction.guild.get_role(role_id)
            if role:
                hr_mentions.append(role.mention)

        mention_text = interaction.user.mention
        if hr_mentions:
            mention_text += "\n" + " ".join(hr_mentions)

        await channel.send(mention_text)

        # ЛОГ СОЗДАНИЯ
        if LOG_CHANNEL_ID:
            log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                log_message = await log_channel.send(embed=embed)
                application["log_message_id"] = log_message.id
                async with data_lock:
                    data = load_data()
                    guild_data = data.setdefault(str(interaction.guild.id), {})
                    user_apps = guild_data.setdefault(str(interaction.user.id), [])
                    if user_apps:
                        user_apps[-1]["log_message_id"] = log_message.id
                        save_data(data)

        await interaction.response.send_message(
            "✅ Ваша заявка отправлена!",
            ephemeral=True
        )

# ================= VIEW =================

class JoinFamilyView(discord.ui.View):

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="📝 Оставить заявку",
        style=discord.ButtonStyle.success,
        custom_id="join_family_button"
    )
    async def join(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(JoinFamilyModal(self.bot))

class ApplicationManageView(discord.ui.View):

    def __init__(self, bot, applicant_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.applicant_id = applicant_id

    async def finalize(self, interaction, status):

        await interaction.response.defer(ephemeral=True)

        async with data_lock:
            data = load_data()
            guild_data = data.get(str(interaction.guild.id))
            if not guild_data:
                await interaction.followup.send("❌ Активная заявка не найдена.", ephemeral=True)
                return

            apps = guild_data.get(str(self.applicant_id))
            if not apps:
                await interaction.followup.send("❌ Активная заявка не найдена.", ephemeral=True)
                return

            application = None

            for app in reversed(apps):
                if not app["closed"]:
                    app["closed"] = True
                    app["status"] = status
                    application = app
                    break

            if application is None:
                await interaction.followup.send("❌ Активная заявка не найдена.", ephemeral=True)
                return

            save_data(data)

        for item in self.children:
            item.disabled = True

        await interaction.message.edit(view=self)

        if LOG_CHANNEL_ID and application:
            log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)

            if log_channel:

                color = 0x2ECC71 if status == "принята" else 0xE74C3C

                result_embed = discord.Embed(
                    title="Заявление",
                    color=color
                )

                result_embed.add_field(name="Ваш ник в игре", value=application["nickname"], inline=False)
                result_embed.add_field(name="Статик #", value=application["static"], inline=False)
                result_embed.add_field(name="Возраст", value=application["age"], inline=False)
                result_embed.add_field(name="Цель вступления", value=application["goal"], inline=False)
                result_embed.add_field(name="Как узнали о семье", value=application["about"], inline=False)

                result_embed.add_field(name="Пользователь", value=f"<@{self.applicant_id}>", inline=False)
                result_embed.add_field(name="Кого", value=f"<@{self.applicant_id}>", inline=True)
                result_embed.add_field(
                    name="Принял" if status == "принята" else "Отклонил",
                    value=interaction.user.mention,
                    inline=True
                )

                result_embed.set_footer(text=now_time())

                log_message = await log_channel.send(embed=result_embed)
                async with data_lock:
                    data = load_data()
                    guild_data = data.get(str(interaction.guild.id), {})
                    apps = guild_data.get(str(self.applicant_id), [])
                    for app in reversed(apps):
                        if app.get("created_at") == application.get("created_at"):
                            app["log_message_id"] = log_message.id
                            break
                    save_data(data)

        await interaction.followup.send(
            f"Заявка {status}. Канал удалится через 5 секунд.",
            ephemeral=True
        )

        await asyncio.sleep(5)
        await interaction.channel.delete()

    # ================= ПРИНЯТЬ =================

    @discord.ui.button(
        label="✅ Принять",
        style=discord.ButtonStyle.success,
        custom_id="family_accept_button"
    )
    async def accept(self, interaction: discord.Interaction, button):

        if not has_hr_access(interaction.user):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return

        guild = interaction.guild
        member = guild.get_member(self.applicant_id)

        if member:

            remove_role = guild.get_role(REMOVE_ROLE_ID) if REMOVE_ROLE_ID else None
            add_role_1 = guild.get_role(ADD_ROLE_1_ID) if ADD_ROLE_1_ID else None
            add_role_2 = guild.get_role(ADD_ROLE_2_ID) if ADD_ROLE_2_ID else None

            try:

                if remove_role:
                    await member.remove_roles(remove_role)

                if add_role_1:
                    await member.add_roles(add_role_1)

                if add_role_2:
                    await member.add_roles(add_role_2)

            except discord.Forbidden:
                logger.warning("Бот не может управлять ролями при принятии заявки %s", self.applicant_id)

            except Exception as e:
                logger.exception("Role error: %s", e)

        await self.finalize(interaction, "принята")

    # ================= ОТКЛОНИТЬ =================

    @discord.ui.button(
        label="❌ Отклонить",
        style=discord.ButtonStyle.danger,
        custom_id="family_decline_button"
    )
    async def decline(self, interaction: discord.Interaction, button):

        if not has_hr_access(interaction.user):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return

        await self.finalize(interaction, "отклонена")

# ================= COG =================

class JoinFamily(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(JoinFamilyView(bot))
        self.bot.loop.create_task(self.restore_active_applications())

    async def restore_active_applications(self):
        await self.bot.wait_until_ready()
        data = load_data()

        for guild_id, users in data.items():

            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue

            for user_id, applications in users.items():
                for app in applications:

                    if app.get("closed"):
                        continue

                    channel_id = app.get("channel_id")
                    if not channel_id:
                        continue

                    channel = guild.get_channel(channel_id)
                    if not channel:
                        continue

                    try:
                        async for message in channel.history(limit=20):
                            if message.author == self.bot.user and message.embeds:
                                await message.edit(
                                    view=ApplicationManageView(self.bot, int(user_id))
                                )
                                break
                    except Exception:
                        continue

    @app_commands.command(
        name="joinfamily",
        description="Создать кнопку подачи заявки в семью"
    )
    async def joinfamily(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        image: discord.Attachment = None
    ):

        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("❌ Команда доступна только на сервере.", ephemeral=True)
            return

        if not has_hr_access(interaction.user):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎭 ЗАЯВКА В СЕМЬЮ",
            description=f"## {title}\n{description}",
            color=0x9B59B6
        )

        if image:
            embed.set_image(url=image.url)

        embed.set_footer(text="Нажмите кнопку ниже, чтобы подать заявку")

        await interaction.channel.send(
            embed=embed,
            view=JoinFamilyView(self.bot)
        )

        await interaction.response.send_message("✅ Кнопка создана.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(JoinFamily(bot))
