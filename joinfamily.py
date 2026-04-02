import discord
import asyncio
import logging
import os
from datetime import datetime
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from runtime_paths import data_path
from json_store import JsonStore
from config import OWNER_ID, MOSCOW_TZ, DATE_FORMAT
from web_config import get_web_config, get_int, get_int_list

load_dotenv()
logger = logging.getLogger(__name__)
data_lock = asyncio.Lock()

# ================= ENV =================

def _get_hr_access() -> list[int]:
    cfg = get_web_config()
    ids = get_int_list(cfg, ["joinfamily", "hr_access"], default=None)
    if ids is not None:
        return ids
    return [
        int(r.strip())
        for r in os.getenv("HR_ACCESS", "").split(",")
        if r.strip().isdigit()
    ]

# ================= CALL CHANNELS =================

def _get_call_channels() -> list[int]:
    cfg = get_web_config()
    ids = get_int_list(cfg, ["joinfamily", "call_channels"], default=None)
    if ids is not None:
        return ids
    return [
        int(channel_id.strip())
        for channel_id in os.getenv("FAMILY_CALL_CHANNELS", "").split(",")
        if channel_id.strip().isdigit()
    ]

def _get_log_channel_id() -> int:
    cfg = get_web_config()
    return get_int(cfg, ["joinfamily", "log_channel_id"], default=int(os.getenv("FAMILY_LOG_CHANNEL", "0"))) or 0


def _get_remove_role_id() -> int:
    cfg = get_web_config()
    return get_int(cfg, ["joinfamily", "remove_role_id"], default=int(os.getenv("FAMILY_REMOVE_ROLE_ID", "0"))) or 0


def _get_add_role_1_id() -> int:
    cfg = get_web_config()
    return get_int(cfg, ["joinfamily", "add_role_1_id"], default=int(os.getenv("FAMILY_ADD_ROLE_1_ID", "0"))) or 0


def _get_add_role_2_id() -> int:
    cfg = get_web_config()
    return get_int(cfg, ["joinfamily", "add_role_2_id"], default=int(os.getenv("FAMILY_ADD_ROLE_2_ID", "0"))) or 0

# ================= FILE =================

_store = JsonStore(data_path("family_applications.json"))


def now_time():
    return datetime.now(MOSCOW_TZ).strftime(DATE_FORMAT)


def has_hr_access(member: discord.Member):
    if member.id == OWNER_ID:
        return True

    allowed_ids = set(_get_hr_access())
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

        # Single lock block: check for existing open app AND create new record
        async with data_lock:
            data = _store.load()
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

            # Create channel inside lock to prevent double-submit race
            category = interaction.channel.category

            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
            }

            for role_id in _get_hr_access():
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

            user_apps.append(application)
            _store.save(data)

        # ================= ПРЕДЫДУЩИЕ ЗАЯВКИ =================

        if previous_apps:
            links = []

            for old in previous_apps:

                emoji = "🟡"

                if old.get("status") == "принята":
                    emoji = "🟢"
                elif old.get("status") == "отклонена":
                    emoji = "🔴"

                log_channel_id = _get_log_channel_id()
                if old.get("log_message_id") and log_channel_id:
                    link = f"https://discord.com/channels/{interaction.guild.id}/{log_channel_id}/{old['log_message_id']}"
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
        for role_id in _get_hr_access():
            role = interaction.guild.get_role(role_id)
            if role:
                hr_mentions.append(role.mention)

        mention_text = interaction.user.mention
        if hr_mentions:
            mention_text += "\n" + " ".join(hr_mentions)

        await channel.send(mention_text)

        # ЛОГ СОЗДАНИЯ
        log_channel_id = _get_log_channel_id()
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                log_message = await log_channel.send(embed=embed)
                application["log_message_id"] = log_message.id
                async with data_lock:
                    data = _store.load()
                    guild_data = data.setdefault(str(interaction.guild.id), {})
                    user_apps = guild_data.setdefault(str(interaction.user.id), [])
                    if user_apps:
                        user_apps[-1]["log_message_id"] = log_message.id
                        _store.save(data)

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
            data = _store.load()
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

            _store.save(data)

        for item in self.children:
            item.disabled = True

        await interaction.message.edit(view=self)

        log_channel_id = _get_log_channel_id()
        if log_channel_id and application:
            log_channel = interaction.guild.get_channel(log_channel_id)

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
                    data = _store.load()
                    guild_data = data.get(str(interaction.guild.id), {})
                    apps = guild_data.get(str(self.applicant_id), [])
                    for app in reversed(apps):
                        if app.get("created_at") == application.get("created_at"):
                            app["log_message_id"] = log_message.id
                            break
                    _store.save(data)

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

            remove_role_id = _get_remove_role_id()
            add_role_1_id = _get_add_role_1_id()
            add_role_2_id = _get_add_role_2_id()

            remove_role = guild.get_role(remove_role_id) if remove_role_id else None
            add_role_1 = guild.get_role(add_role_1_id) if add_role_1_id else None
            add_role_2 = guild.get_role(add_role_2_id) if add_role_2_id else None

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
        self._tasks: set[asyncio.Task] = set()
        self.bot.add_view(JoinFamilyView(bot))
        self._track_task(self.restore_active_applications())

    def _track_task(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cog_unload(self):
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def restore_active_applications(self):
        await self.bot.wait_until_ready()
        data = _store.load()

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
    @app_commands.describe(
        title="Заголовок блока с заявкой",
        description="Описание или условия вступления",
        image="Картинка для баннера заявки"
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
